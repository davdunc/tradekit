"""DAS Trader depth source: the primary in ``auto``. NOT IMPLEMENTED YET (#9).

This adapter must be finished, and tested against live DAS, on a host running DAS Trader
(``daslaptop``). Until then ``connect()`` raises ``NotImplementedError``; the recorder treats
that as "unavailable" once and records from the IBKR fallback.

What finishing it involves:

1. **Transport.** DAS's CMD API binds loopback on the DAS host. From ``toolsmith`` reach it
   through an SSH local forward from ``daslaptop`` (for example
   ``ssh -N -L 9800:127.0.0.1:<das-port> daslaptop``), never a port opened to the network:
   the CMD API can place orders.
2. **Read-only.** Send only login and market-data commands. Never send order commands; the
   adapter should refuse to, so a bug can't trade.
3. **Level 2.** Subscribe to Level 2 / market-maker quotes per symbol and parse each update
   into ``BookUpdate`` (side, position or price level, op, price, size, market-maker id as
   ``venue``). The message format comes from DAS's CMD API documentation or the existing
   ``das_quote.py`` (which subscribes Level 1 only today).
4. **Disconnects.** End ``events()`` (or raise) when the socket closes, so the recorder fails
   over to IBKR at once.
5. **Tests.** Record a few minutes of raw CMD API output as a fixture and test the parser
   against it, as ``test_depth_sources.py`` does for the IBKR and Alpaca mappings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from tradekit.depth.events import Event


class DASSource:
    name = "das"

    async def connect(self) -> None:
        raise NotImplementedError("DAS adapter not implemented yet: finish it on a host running DAS Trader (see #9)")

    async def subscribe(self, symbols: Sequence[str]) -> None:  # pragma: no cover
        raise NotImplementedError

    def events(self) -> AsyncIterator[Event]:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None
