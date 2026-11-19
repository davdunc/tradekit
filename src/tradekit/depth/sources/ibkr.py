"""IBKR depth source through IB Gateway (TWS API ``reqMktDepth``): the fallback in ``auto``.

Uses ``ib_async`` (``tradekit-cli[ibkr]``). Connects **read-only**, so even a bug can't place an
order; set the Gateway's "Read-Only API" too. IB Gateway runs headless on Linux, so this
source can run on ``toolsmith`` itself.

Limits to know: depth needs IBKR market-data subscriptions for the exchanges involved, and
IBKR caps concurrent depth subscriptions (a small number by default). Every TWS message except
known-harmless status codes is recorded as an ``error`` marker, so a refused subscription can
never look like an empty book. Seen live (2026-11-19, paper account without depth
subscriptions): 2152 "Need additional market data permissions - Depth: BATS; NASDAQ; ARCA; …"
for SMART depth, and 10089 "requires additional subscription for API … NASDAQ.NMS/DEEP"
followed by 310 for a direct exchange.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from tradekit.depth.events import FULL_DEPTH, BookUpdate, Event, Marker, Op, Side

logger = logging.getLogger(__name__)

# TWS API updateMktDepth(L2): side 0 = ask, 1 = bid; operation 0 insert, 1 update, 2 delete.
_SIDE: dict[int, Side] = {0: "ask", 1: "bid"}
_OP: dict[int, Op] = {0: "insert", 1: "update", 2: "delete"}

#: TWS status messages that are not about our data: data-farm connection OK / inactive / reconnected.
#: Every other code becomes a marker. An allowlist of *errors* missed the real refusals (2152,
#: 10089) and recorded an empty book with no explanation; this way an unknown code is visible.
STATUS_CODES = {2104, 2106, 2107, 2108, 2119, 2158}


def book_update_from_dom(symbol: str, tick: Any) -> BookUpdate | None:
    """Map one ib_async ``MktDepthData`` to a ``BookUpdate``; ``None`` if it's malformed."""
    raw_side, raw_op = getattr(tick, "side", None), getattr(tick, "operation", None)
    if not isinstance(raw_side, int) or not isinstance(raw_op, int):
        return None
    side, op = _SIDE.get(raw_side), _OP.get(raw_op)
    if side is None or op is None:
        return None
    price = getattr(tick, "price", None)
    size = getattr(tick, "size", None)
    return BookUpdate(
        source="ibkr",
        symbol=symbol,
        side=side,
        position=int(getattr(tick, "position", 0)),
        op=op,
        price=None if op == "delete" else float(price) if price is not None else None,
        size=None if op == "delete" else float(size) if size is not None else None,
        depth_levels=FULL_DEPTH,
        venue=str(getattr(tick, "marketMaker", "") or ""),
    )


def missing_settings(host: str, port: int | None) -> list[str]:
    """The IB Gateway connection variables that aren't set. Neither has a default (SPEC G12)."""
    return [name for name, value in (("IBKR_HOST", host), ("IBKR_PORT", port)) if not value]


def missing_settings_message(missing: Sequence[str]) -> str:
    return (
        f"IBKR source needs {' and '.join(missing)} "
        "(IB Gateway: 4002 paper, 4001 live; TWS: 7497 paper, 7496 live). "
        "There is no default: a guessed port would be refused and look like IBKR being down"
    )


def marker_from_tws_error(req_id: int, code: int, message: str, contract: Any = None) -> Marker | None:
    """A TWS error/status message as an ``error`` marker; ``None`` for harmless status codes."""
    if code in STATUS_CODES:
        return None
    symbol = getattr(contract, "symbol", "") or ""
    return Marker("error", "ibkr", f"{symbol} TWS {code} (req {req_id}): {message}".strip())


class IBKRSource:
    name = "ibkr"

    def __init__(self, host: str, port: int | None, client_id: int = 17, *, levels: int = 10):
        self.host, self.port, self.client_id, self.levels = host, port, client_id, levels
        self._ib: Any = None
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._symbol_of: dict[int, str] = {}  # contract conId -> symbol

    async def connect(self) -> None:
        missing = missing_settings(self.host, self.port)
        if missing:
            from tradekit.depth.recorder import PermanentlyUnavailable

            raise PermanentlyUnavailable(missing_settings_message(missing))
        try:
            from ib_async import IB
        except ImportError as e:
            from tradekit.depth.sources import MissingExtra

            raise MissingExtra("IBKR source", "ibkr", "ib_async") from e
        # A fresh queue per connection: the previous connection's disconnect sentinel, which
        # arrives after close(), must not end this one.
        self._queue = asyncio.Queue()
        self._ib = IB()
        await self._ib.connectAsync(self.host, self.port, clientId=self.client_id, readonly=True, timeout=10)
        self._ib.pendingTickersEvent += self._on_tickers
        self._ib.errorEvent += self._on_error
        self._ib.disconnectedEvent += lambda: self._queue.put_nowait(None)

    async def subscribe(self, symbols: Sequence[str]) -> None:
        from ib_async import Stock

        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        qualified = await self._ib.qualifyContractsAsync(*contracts)
        missing = {s for s in symbols} - {c.symbol for c in qualified if c}
        for s in sorted(missing):
            self._queue.put_nowait(Marker("error", self.name, f"{s}: contract not found"))
        for contract in qualified:
            if not contract:
                continue
            self._symbol_of[contract.conId] = contract.symbol
            self._ib.reqMktDepth(contract, numRows=self.levels, isSmartDepth=True)

    def describe(self) -> str:
        return f"{self.host}:{self.port} client {self.client_id}, {self.levels} levels, read-only"

    def _on_tickers(self, tickers) -> None:
        for ticker in tickers:
            con_id = getattr(ticker.contract, "conId", 0) or 0
            symbol = str(self._symbol_of.get(con_id) or ticker.contract.symbol)
            for tick in getattr(ticker, "domTicks", None) or ():
                update = book_update_from_dom(symbol, tick)
                if update is not None:
                    self._queue.put_nowait(update)

    def _on_error(self, req_id, code, message, contract=None) -> None:
        marker = marker_from_tws_error(req_id, code, message, contract)
        if marker is not None:
            self._queue.put_nowait(marker)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            if event is None:  # disconnected
                return
            yield event

    async def close(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
