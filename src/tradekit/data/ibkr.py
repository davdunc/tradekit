"""Price, volatility, dividend and borrow snapshot from IB Gateway, for the overnight-risk check.

One read-only connection, one ``reqMktData`` with generic ticks 104 (30-day historical
volatility), 106 (30-day implied volatility), 236 (shortable level and shares) and 456 (IB
dividends: next ex-date and amount). Needs ``tradekit-cli[ibkr]`` and live US equity and options
data on the IBKR username (e.g. the US Securities Snapshot and Futures Value Bundle plus the
Equity and Options Add-On).

Freshness is stated, never assumed (SPEC, the data provider contract). The request asks for
market data type 1. Type 2 (frozen) was tried first and dropped: measured 2026-11-21, it
returned no shortable or dividend ticks for SPY and GME, where type 1 returned all of them.
Outside the session type 1 still answers, with the last values, so ``live`` means a live
subscription feed, not an open market. Delayed data (3/4) is refused rather than passed off.

Missing is kept distinct from none: ``dividends_known`` is False when tick 456 never arrived,
and True with ``ex_dividend_date`` None when the broker reports no scheduled dividend.

Connection settings follow the no-fallback rule: ``IBKR_HOST`` and ``IBKR_PORT`` must be set.
The client id is ``IBKR_CLIENT_ID + 1``, so a snapshot never collides with a running
``tradekit depth record`` session on the same Gateway.
"""

from __future__ import annotations

import asyncio
import datetime as dt
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
    dividends_known: bool = False  # tick 456 arrived
    ex_dividend_date: dt.date | None = None
    dividend_amount: float | None = None
    shortable: float | None = None  # tick 236 level; None when it never arrived
    shortable_shares: int | None = None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and math.isfinite(value) and value > 0 else None


def _finite(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and math.isfinite(value) else None


def _done(has_required: bool, has_all: bool, elapsed: float, grace: float) -> bool:
    """Stop waiting once every field is in, or once price and IV are in and ``grace`` has passed.

    HV, borrow and dividends are optional: some never arrive (HV for an ETF in frozen weekend
    data, for one). Waiting for them until the deadline made every such call take the full
    ``wait`` although price and IV arrived at once (#45 review).
    """
    return has_all or (has_required and elapsed >= grace)


async def _snapshot(symbol: str, host: str, port: int, client_id: int, wait: float, grace: float = 2.0) -> VolSnapshot:
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
        ib.reqMarketDataType(1)
        ticker = ib.reqMktData(contract, "104,106,236,456", False, False)
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < wait:
            required = bool(
                (_number(ticker.marketPrice()) or _number(ticker.close)) and _number(ticker.impliedVolatility)
            )
            optional = (
                _number(ticker.histVolatility) is not None
                and _finite(ticker.shortable) is not None
                and ticker.dividends is not None
            )
            if _done(required, required and optional, loop.time() - start, grace):
                break
            await asyncio.sleep(0.1)
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
        divs = ticker.dividends
        shortable = _finite(ticker.shortable)
        shares = _finite(ticker.shortableShares)
        return VolSnapshot(
            symbol,
            round(price, 4),
            round(iv, 6),
            _number(ticker.histVolatility),
            freshness,
            dividends_known=divs is not None,
            ex_dividend_date=getattr(divs, "nextDate", None),
            dividend_amount=_number(getattr(divs, "nextAmount", None)),
            shortable=shortable,
            shortable_shares=None if shares is None else int(shares),
        )
    finally:
        ib.disconnect()


def fetch_vol_snapshot(symbol: str, *, wait: float = 8.0, grace: float = 2.0) -> VolSnapshot:
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
    return asyncio.run(_snapshot(symbol.upper(), data.ibkr_host, int(data.ibkr_port), client_id, wait, grace))
