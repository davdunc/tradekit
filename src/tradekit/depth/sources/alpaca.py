"""Alpaca level 1 source: top-of-book quotes and trades. Explicit opt-in only, never a fallback.

Alpaca's stock stream has no order book (checked 2026-11-19: trades, quotes, bars, statuses,
LULDs, imbalances; order books exist only for crypto). Each quote becomes two ``BookUpdate``
rows at position 0 with ``depth_levels=1``, so it can never pass for depth. Real-time coverage
depends on the plan: ``iex`` (free, IEX only) or ``sip`` (Algo Trader Plus, all US exchanges).

Uses ``alpaca-py`` (``tradekit-cli[alpaca]``) and its public ``StockDataStream.run()``, which
blocks, so the stream runs in a worker thread and hands events to the recorder's loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from tradekit.depth.events import BookUpdate, Event, Marker, Side, Trade

logger = logging.getLogger(__name__)

LEVEL_1 = 1


def book_updates_from_quote(quote: Any) -> list[BookUpdate]:
    """An Alpaca quote → bid and ask ``BookUpdate`` rows at position 0, ``depth_levels=1``."""
    rows = []
    sides: tuple[tuple[Side, Any, Any, Any], ...] = (
        ("bid", quote.bid_price, quote.bid_size, getattr(quote, "bid_exchange", "")),
        ("ask", quote.ask_price, quote.ask_size, getattr(quote, "ask_exchange", "")),
    )
    for side, price, size, venue in sides:
        rows.append(
            BookUpdate(
                source="alpaca",
                symbol=quote.symbol,
                side=side,
                position=0,
                op="update",
                price=float(price) if price is not None else None,
                size=float(size) if size is not None else None,
                depth_levels=LEVEL_1,
                venue=str(venue or ""),
            )
        )
    return rows


def trade_from_alpaca(trade: Any) -> Trade:
    return Trade(
        source="alpaca",
        symbol=trade.symbol,
        price=float(trade.price),
        size=float(trade.size),
        venue=str(getattr(trade, "exchange", "") or ""),
    )


class AlpacaSource:
    name = "alpaca"

    def __init__(self, key_id: str, secret_key: str, *, feed: str = "iex"):
        self.key_id, self.secret_key, self.feed = key_id, secret_key, feed.lower()
        self._stream: Any = None
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: asyncio.Future | None = None

    async def connect(self) -> None:
        if not (self.key_id and self.secret_key):
            raise RuntimeError("APCA_API_KEY_ID and APCA_API_SECRET_KEY are not set")
        if self.feed not in ("iex", "sip"):
            raise RuntimeError(f"alpaca_feed must be 'iex' or 'sip', not {self.feed!r}")
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.live import StockDataStream
        except ImportError as e:
            from tradekit.depth.sources import MissingExtra

            raise MissingExtra("Alpaca source", "alpaca", "alpaca-py") from e
        self._loop = asyncio.get_running_loop()
        # A fresh queue per connection: a previous stream's end-of-stream sentinel, which can
        # arrive after close(), must not end this one.
        self._queue = asyncio.Queue()
        feed = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
        self._stream = StockDataStream(self.key_id, self.secret_key, feed=feed)

    def _put(self, event: Event | None) -> None:
        """Hand an event to the recorder's loop from the stream's thread."""
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def subscribe(self, symbols: Sequence[str]) -> None:
        async def on_quote(quote) -> None:
            for row in book_updates_from_quote(quote):
                self._put(row)

        async def on_trade(trade) -> None:
            self._put(trade_from_alpaca(trade))

        self._stream.subscribe_quotes(on_quote, *symbols)
        self._stream.subscribe_trades(on_trade, *symbols)

    def describe(self) -> str:
        return f"level 1 only, feed={self.feed}"

    def _run_stream(self) -> None:
        try:
            self._stream.run()
        except Exception as e:  # noqa: BLE001 - reported as a marker, then the stream ends
            self._put(Marker("error", self.name, f"{type(e).__name__}: {e}"))
        finally:
            self._put(None)

    async def events(self) -> AsyncIterator[Event]:
        self._runner = asyncio.ensure_future(asyncio.to_thread(self._run_stream))
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:  # noqa: BLE001
                logger.debug("alpaca stream stop failed", exc_info=True)
