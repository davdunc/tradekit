"""Price and volatility snapshot from IB Gateway, for the overnight-risk check.

One read-only connection: price, 30-day implied volatility (generic tick 106) and 30-day
historical volatility (tick 104) for a stock or ETF. Needs ``tradekit-cli[ibkr]`` and live US
equity and options data on the IBKR username (e.g. the US Securities Snapshot and Futures Value
Bundle plus the Equity and Options Add-On).

Freshness is stated, never assumed (SPEC, the data provider contract). The request asks for
market data type 2: live during the session, otherwise TWS's frozen last values. The type that
actually arrived is returned, and delayed data (3/4) is refused rather than passed off as live.

Connection settings follow the no-fallback rule: ``IBKR_HOST`` and ``IBKR_PORT`` must be set.
The client id is ``IBKR_CLIENT_ID + 1``, so a snapshot never collides with a running
``tradekit depth record`` session on the same Gateway.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

#: TWS market data types (reqMarketDataType).
MARKET_DATA_TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
#: Data-farm status messages: informational, not about the request.
_STATUS_CODES = {2104, 2106, 2107, 2108, 2119, 2158}


class IBKRSnapshotError(RuntimeError):
    """The snapshot could not be taken; the message says why."""


@dataclass(frozen=True)
class VolSnapshot:
    symbol: str
    price: float
    iv: float
    hv: float | None
    freshness: str  # live | frozen

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "price": self.price, "iv": self.iv, "hv": self.hv, "freshness": self.freshness}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and math.isfinite(value) and value > 0 else None


async def _snapshot(symbol: str, host: str, port: int, client_id: int, wait: float) -> VolSnapshot:
    from ib_async import IB, Stock

    ib = IB()
    refusals: list[str] = []
    ib.errorEvent += lambda req_id, code, msg, contract=None: (
        refusals.append(f"TWS {code}: {msg}") if code not in _STATUS_CODES else None
    )
    try:
        await ib.connectAsync(host, port, clientId=client_id, readonly=True, timeout=10)
    except Exception as e:  # noqa: BLE001 - every connect failure is reported the same way
        raise IBKRSnapshotError(f"IB Gateway at {host}:{port} refused the connection ({type(e).__name__}: {e})") from e
    try:
        contract = Stock(symbol, "SMART", "USD")
        if not await ib.qualifyContractsAsync(contract) or not contract.conId:
            raise IBKRSnapshotError(f"{symbol}: no such US stock or ETF at IBKR")
        ib.reqMarketDataType(2)
        ticker = ib.reqMktData(contract, "104,106", False, False)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while loop.time() < deadline:  # stop early once price, IV and HV have all arrived
            has_price = _number(ticker.marketPrice()) or _number(ticker.close)
            if has_price and _number(ticker.impliedVolatility) and _number(ticker.histVolatility):
                break
            await asyncio.sleep(0.25)
        ib.cancelMktData(contract)

        md_type = ticker.marketDataType
        freshness = MARKET_DATA_TYPES.get(md_type, f"type {md_type}")
        if md_type not in (1, 2):
            raise IBKRSnapshotError(
                f"{symbol}: IBKR sent {freshness} data; the check needs live or frozen (is the live username "
                "logged in elsewhere, or the subscription missing?)"
            )
        price = _number(ticker.marketPrice()) or _number(ticker.last) or _number(ticker.close)
        iv = _number(ticker.impliedVolatility)
        if price is None or iv is None:
            missing = " and ".join(n for n, v in (("price", price), ("implied volatility", iv)) if v is None)
            detail = f" ({'; '.join(refusals)})" if refusals else ""
            raise IBKRSnapshotError(f"{symbol}: IBKR returned no {missing} within {wait:g}s{detail}")
        return VolSnapshot(symbol, round(price, 4), round(iv, 6), _number(ticker.histVolatility), freshness)
    finally:
        ib.disconnect()


def fetch_vol_snapshot(symbol: str, *, wait: float = 8.0) -> VolSnapshot:
    """Price, IV and HV for ``symbol`` from IB Gateway. Raises ``IBKRSnapshotError`` with the reason."""
    from tradekit.config import get_settings

    data = get_settings().data
    missing = [name for name, value in (("IBKR_HOST", data.ibkr_host), ("IBKR_PORT", data.ibkr_port)) if not value]
    if missing:
        raise IBKRSnapshotError(
            f"set {' and '.join(missing)} for IB Gateway (4002 paper, 4001 live), or pass --price and --iv"
        )
    try:
        import ib_async  # noqa: F401
    except ImportError as e:
        raise IBKRSnapshotError(
            "IBKR snapshot needs ib_async: install with `uv tool install 'tradekit-cli[ibkr]'`, "
            "or pass --price and --iv"
        ) from e
    client_id = data.ibkr_client_id + 1
    return asyncio.run(_snapshot(symbol.upper(), data.ibkr_host, int(data.ibkr_port), client_id, wait))
