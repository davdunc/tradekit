"""Read-only DAS Trader CMD API connection, and the probe that answers #9's open questions.

DAS's CMD API carries market data and order entry on one socket, so this module makes the
connection unable to trade in two independent ways (#9, "WATCH mode" amendment):

1. **WATCH login.** ``LOGIN <user> <password> <account> 1``: the trailing ``1`` makes it a watch
   connection, which per the CMD API manual cannot place orders. :func:`login_line` always adds
   it, and there is no way to build a bare login here.
2. **A command allowlist.** :meth:`WatchConnection.send` writes only ``LOGIN``, ``SB`` and
   ``UNSB`` lines, checked before a byte is written. ``NEWORDER``, ``CANCEL``, ``SCRIPT`` (hotkey
   scripts can place orders) and anything else raise :class:`ReadOnlyViolation`. A line holding a
   CR or LF is refused too, so a second command can't be smuggled into one ``send``.

What isn't known yet, and why the probe exists: the Level 2 message format, and whether DAS
streams symbols that aren't loaded in a montage. The DAS reference on this machine even says the
API has no Level 2. ``tradekit depth das-probe`` subscribes a few symbols, records every line
sent and received to a JSONL capture (password redacted), and prints a summary. That capture is
the evidence for both questions and the fixture the DAS ``DepthSource`` parser will be tested on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: The only command verbs this connection will send. Market data and login, nothing else.
ALLOWED_VERBS = frozenset({"LOGIN", "SB", "UNSB"})
#: Subscription kinds the probe tries by default. ``Lv2`` is the one #9 needs; it may not exist.
DEFAULT_KINDS = ("Lv1", "Lv2", "tms")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
_KIND = re.compile(r"^[A-Za-z0-9]{1,12}$")
#: Words in a reply that mean the login was refused. Matched case-insensitively.
_REFUSAL = re.compile(r"\b(fail\w*|invalid|denied|error|wrong|reject\w*)\b", re.IGNORECASE)


class ReadOnlyViolation(Exception):
    """A command that isn't login or market data. Raised before anything reaches DAS."""


def check_command(line: str) -> str:
    """Return ``line`` if this connection may send it, else raise :class:`ReadOnlyViolation`."""
    if "\r" in line or "\n" in line:
        raise ReadOnlyViolation("a command must be a single line")
    verb = line.split(maxsplit=1)[0].upper() if line.strip() else ""
    if verb not in ALLOWED_VERBS:
        raise ReadOnlyViolation(f"{verb or 'an empty line'} is not allowed: this connection is read-only")
    if verb == "LOGIN":
        tokens = line.split()
        if len(tokens) != 5 or tokens[4] != "1":
            raise ReadOnlyViolation("LOGIN must be exactly a WATCH login: LOGIN <user> <password> <account> 1")
    return line


def login_line(user: str, password: str, account: str) -> str:
    """A WATCH-mode login. The trailing ``1`` is not optional."""
    for name, value in (("user", user), ("password", password), ("account", account)):
        if not value or any(c.isspace() for c in value):
            raise ValueError(f"DAS {name} must be set and contain no whitespace")
    return f"LOGIN {user} {password} {account} 1"


def subscribe_line(symbol: str, kind: str) -> str:
    symbol = symbol.upper()
    if not _SYMBOL.match(symbol):
        raise ValueError(f"{symbol!r} is not a ticker symbol")
    if not _KIND.match(kind):
        raise ValueError(f"{kind!r} is not a subscription kind")
    return f"SB {symbol} {kind}"


def login_refused(reply: str) -> bool:
    return bool(_REFUSAL.search(reply))


class WatchConnection:
    """One read-only CMD API socket. Every outgoing line goes through :func:`check_command`."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader, self._writer = reader, writer

    @classmethod
    async def open(cls, host: str, port: int, timeout: float = 10.0) -> WatchConnection:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        return cls(reader, writer)

    async def send(self, line: str) -> None:
        check_command(line)  # before the write, always
        self._writer.write(line.encode("ascii") + b"\r\n")
        await self._writer.drain()

    async def lines(self) -> AsyncIterator[str]:
        """Received lines until the socket closes."""
        while True:
            raw = await self._reader.readline()
            if not raw:
                return
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError, OSError:
            pass


@dataclass
class ProbeResult:
    """What a probe saw. ``capture`` is the JSONL file, the evidence to attach to #9."""

    capture: Path
    login_reply: list[str] = field(default_factory=list)
    login_refused: bool = False
    closed_by_server: bool = False
    sent: list[str] = field(default_factory=list)
    received: int = 0
    first_tokens: Counter = field(default_factory=Counter)
    lines_per_symbol: Counter = field(default_factory=Counter)


async def probe(
    conn: WatchConnection,
    *,
    login: str,
    password: str,
    symbols: Sequence[str],
    kinds: Sequence[str] = DEFAULT_KINDS,
    capture: Path,
    login_wait: float = 5.0,
    listen: float = 60.0,
    pace: float = 1.0,
    clock=lambda: dt.datetime.now(dt.UTC),
) -> ProbeResult:
    """WATCH-login, subscribe each symbol to each kind, and record everything for ``listen`` s.

    Stops after the login reply if it reads as a refusal, without subscribing. Every line is
    written to ``capture`` as ``{"t", "dir": "sent"|"recv", "line"}`` with the password redacted.
    """
    capture.parent.mkdir(parents=True, exist_ok=True)
    result = ProbeResult(capture=capture)
    wanted = [s.upper() for s in symbols]
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def pump() -> None:
        async for line in conn.lines():
            await queue.put(line)
        await queue.put(None)

    with capture.open("w", encoding="utf-8") as fh:

        def write(direction: str, line: str) -> None:
            shown = line.replace(password, "***") if password else line
            fh.write(json.dumps({"t": clock().isoformat(), "dir": direction, "line": shown}) + "\n")
            fh.flush()

        async def send(line: str) -> None:
            await conn.send(line)
            write("sent", line)
            result.sent.append(line.replace(password, "***") if password else line)

        async def drain(seconds: float, *, during_login: bool = False) -> bool:
            """Record received lines for ``seconds``. False once the server has closed."""
            loop = asyncio.get_running_loop()
            deadline = loop.time() + seconds
            while (left := deadline - loop.time()) > 0:
                try:
                    line = await asyncio.wait_for(queue.get(), left)
                except TimeoutError:
                    return True
                if line is None:
                    result.closed_by_server = True
                    return False
                write("recv", line)
                result.received += 1
                if line.strip():
                    result.first_tokens[line.split()[0]] += 1
                tokens = set(re.split(r"[\s,]+", line.upper()))
                for s in wanted:
                    if s in tokens:
                        result.lines_per_symbol[s] += 1
                if during_login:
                    result.login_reply.append(line)
            return True

        reader = asyncio.create_task(pump())
        try:
            await send(login)
            alive = await drain(login_wait, during_login=True)
            result.login_refused = any(login_refused(r) for r in result.login_reply)
            if not alive or result.login_refused:
                return result
            for s in wanted:
                for kind in kinds:
                    await send(subscribe_line(s, kind))
                    if not await drain(pace):
                        return result
            await drain(listen)
        finally:
            reader.cancel()
            await conn.close()
    return result
