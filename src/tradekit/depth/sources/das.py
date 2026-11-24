"""DAS Trader depth source: the primary in ``auto`` (#9).

Runs on the DAS machine: DAS binds loopback, so ``DAS_HOST`` is 127.0.0.1 there. The transport
is :class:`tradekit.depth.das_cmd.WatchConnection`, which logs in in WATCH mode (it can't place
orders) and refuses every command but ``LOGIN``/``SB``/``UNSB``.

What DAS sends, from the probe captures on #9 (overnight 2026-11-23 03:00Z, premarket 09:11Z):

- **Login.** ``#LOGIN SUCCESSED`` (sic), then the ``#IPOS`` position header, the "I" form a
  WATCH connection gets. Both are required before subscribing; otherwise the source refuses to
  run for this session, rather than record on a connection that might be able to trade.
- **``$Lv2 <symb> <A|B> <mmid> <price> <size> <action> <HH:MM:SS>``**: one row per venue and
  side (a market-maker montage), not per price level. On subscribe DAS sends each venue's
  current quote, then changes. Actions seen: ``UPDATED`` (also how a venue first appears; no
  ``ADDED`` was seen) and ``REMOVED`` (price and size ``0 0`` in premarket, last values
  overnight). The stamp is the DAS host's *local* time of the venue's last change, not ET and
  not the event time, so rows are timed by receipt.
- **``$T&S <symb> <price> <size> <flag> <HH:MM:SS ET> <venue> <I|S> <code>``**: prints. The
  subscription replays recent prints, so a trade is timed by its ET stamp, not by receipt. The
  flag, ``I|S`` and code meanings aren't known yet, so they aren't recorded.

Mapping to events: ``UPDATED`` is an ``insert`` the first time a (symbol, side, venue) appears
on this connection and an ``update`` after; ``REMOVED`` is a ``delete`` (a ``0`` price or size
becomes None). Rows carry ``position=VENUE_KEYED`` and the venue. Sizes are as DAS sends them;
whether that unit is shares or round lots is a DAS display setting, unconfirmed. An unknown
action or a malformed line becomes an ``error`` marker, once per distinct problem.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Sequence
from zoneinfo import ZoneInfo

from tradekit.depth.events import VENUE_KEYED, BookUpdate, Event, Marker, Trade, utcnow

ET = ZoneInfo("America/New_York")
LOGIN_OK = "#LOGIN SUCCESSED"
WATCH_HEADER = "#IPOS"
SIDES = {"B": "bid", "A": "ask"}
KINDS = ("Lv2", "tms")  # what the recorder subscribes; Level 1 is not depth
NAME = "das"


def _number(text: str) -> float | None:
    value = float(text)
    return value or None


class LineParser:
    """Turns DAS lines into events. Stateful only in which (symbol, side, venue) it has seen."""

    def __init__(self, source: str = NAME):
        self.source = source
        self._seen: set[tuple[str, str, str]] = set()
        self._reported: set[str] = set()

    def _problem(self, text: str, ts: dt.datetime) -> list[Event]:
        if text in self._reported:
            return []
        self._reported.add(text)
        return [Marker("error", self.source, text, ts=ts)]

    def parse(self, line: str, ts: dt.datetime | None = None) -> list[Event]:
        ts = ts or utcnow()
        if line.startswith("$Lv2 "):
            return self._lv2(line, ts)
        if line.startswith("$T&S "):
            return self._tms(line, ts)
        return []  # banners, $Quote and anything else: not depth

    def _lv2(self, line: str, ts: dt.datetime) -> list[Event]:
        parts = line.split()
        if len(parts) != 8 or parts[2] not in SIDES:
            return self._problem(f"unparsed $Lv2 line: {line}", ts)
        _, symbol, side_code, venue, price_text, size_text, action, _stamp = parts
        try:
            price, size = _number(price_text), _number(size_text)
        except ValueError:
            return self._problem(f"unparsed $Lv2 line: {line}", ts)
        side = SIDES[side_code]
        key = (symbol, side, venue)
        if action == "UPDATED":
            op = "update" if key in self._seen else "insert"
            self._seen.add(key)
        elif action == "REMOVED":
            op = "delete"
            self._seen.discard(key)
        else:
            return self._problem(f"unknown $Lv2 action {action!r}: {line}", ts)
        return [BookUpdate(self.source, symbol, side, VENUE_KEYED, op, price, size, venue=venue, ts=ts)]

    def _tms(self, line: str, ts: dt.datetime) -> list[Event]:
        parts = line.split()
        if len(parts) < 7:
            return self._problem(f"unparsed $T&S line: {line}", ts)
        symbol, price_text, size_text, stamp, venue = parts[1], parts[2], parts[3], parts[5], parts[6]
        try:
            price, size = float(price_text), float(size_text)
            when = dt.time.fromisoformat(stamp)
        except ValueError:
            return self._problem(f"unparsed $T&S line: {line}", ts)
        return [Trade(self.source, symbol, price, size, venue=venue, ts=trade_time(when, ts))]


def trade_time(stamp: dt.time, received: dt.datetime) -> dt.datetime:
    """A print's ET stamp on the receive date, in UTC. A stamp later than receipt is yesterday's
    (the overnight session crosses midnight ET)."""
    local = received.astimezone(ET)
    when = dt.datetime.combine(local.date(), stamp, tzinfo=ET)
    if when > local + dt.timedelta(minutes=1):
        when -= dt.timedelta(days=1)
    return when.astimezone(dt.UTC)


def missing_settings(host, port, user, password, account) -> list[str]:
    from tradekit.config import env_var

    values = {
        "das_host": host,
        "das_port": port,
        "das_user": user,
        "das_password": password,
        "das_account": account,
    }
    return [env_var(name) for name, value in values.items() if not value]


class DASSource:
    name = NAME

    def __init__(self, host: str, port: int | None, user: str, password: str, account: str, *, login_wait=10.0):
        self.host, self.port, self.user, self.password, self.account = host, port, user, password, account
        self.login_wait = login_wait
        self._conn = None
        self._lines = None
        self._parser = LineParser()

    def describe(self) -> str:
        return f"{self.host}:{self.port}, WATCH (read-only)"

    async def connect(self) -> None:
        from tradekit.depth import das_cmd
        from tradekit.depth.recorder import PermanentlyUnavailable

        missing = missing_settings(self.host, self.port, self.user, self.password, self.account)
        if missing:
            raise PermanentlyUnavailable(
                f"DAS source needs {', '.join(missing)} (on the DAS machine; the host is 127.0.0.1)"
            )
        login = das_cmd.login_line(self.user, self.password, self.account)
        conn = await das_cmd.WatchConnection.open(self.host, self.port)
        lines = conn.lines()
        await conn.send(login)
        seen_ok = seen_watch = False
        try:
            async with asyncio.timeout(self.login_wait):
                async for line in lines:
                    if line.startswith(LOGIN_OK):
                        seen_ok = True
                    elif das_cmd.login_refused(line):
                        break
                    if seen_ok and line.startswith(WATCH_HEADER):
                        seen_watch = True
                        break
        except TimeoutError:
            pass
        if not (seen_ok and seen_watch):
            await conn.close()
            why = "login refused or unanswered" if not seen_ok else "WATCH mode not confirmed (no #IPOS)"
            raise PermanentlyUnavailable(f"DAS: {why}; not recording on this connection")
        self._parser = LineParser()  # venue state is per connection
        self._conn, self._lines = conn, lines

    async def subscribe(self, symbols: Sequence[str]) -> None:
        from tradekit.depth import das_cmd

        for symbol in symbols:
            for kind in KINDS:
                await self._conn.send(das_cmd.subscribe_line(symbol, kind))

    async def events(self) -> AsyncIterator[Event]:
        async for line in self._lines:  # ends when DAS closes the socket: the recorder fails over
            for event in self._parser.parse(line):
                yield event

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
