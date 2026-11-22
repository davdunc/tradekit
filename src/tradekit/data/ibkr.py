"""Read-only IB Gateway snapshots: ``fetch_vol_snapshot`` and ``fetch_event_chain``.

- ``fetch_vol_snapshot`` gives price, volatility, dividend and borrow for ``tradekit overnight``.
- ``fetch_event_chain`` gives the option quotes for ``tradekit earnings``' event straddle. It
  replaces CBOE's delayed-quotes chain, which now blocks automated access (#55).

``fetch_vol_snapshot``: one read-only connection, one ``reqMktData`` with generic ticks 104 (30-day historical
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
from collections.abc import Callable
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


def _gateway(hint: str) -> tuple[str, int, int]:
    """(host, port, client id) for a read-only snapshot, or IBKRSnapshotError saying what to set."""
    from tradekit.config import get_settings

    data = get_settings().data
    missing = [name for name, value in (("IBKR_HOST", data.ibkr_host), ("IBKR_PORT", data.ibkr_port)) if not value]
    if missing:
        raise IBKRSnapshotError(f"set {' and '.join(missing)} for IB Gateway (4002 paper, 4001 live){hint}")
    try:
        import ib_async  # noqa: F401
    except ImportError as e:
        raise IBKRSnapshotError(
            f"IBKR snapshot needs ib_async: install with `uv tool install 'tradekit-cli[ibkr]'`{hint}"
        ) from e
    return data.ibkr_host, int(data.ibkr_port), data.ibkr_client_id + 1


def fetch_vol_snapshot(symbol: str, *, wait: float = 8.0, grace: float = 2.0) -> VolSnapshot:
    """Price, IV and HV for ``symbol`` from IB Gateway. Raises ``IBKRSnapshotError`` with the reason."""
    host, port, client_id = _gateway(", or pass --price and --iv")
    return asyncio.run(_snapshot(symbol.upper(), host, port, client_id, wait, grace))


# ----- option quotes for the earnings straddle (#55: CBOE now blocks automated access) -----


def _staleness(label: str) -> int:
    order = ("live", "frozen", "delayed", "delayed-frozen")
    return order.index(label) if label in order else len(order)


def _nearest(values, target: float, n: int) -> list[float]:
    return sorted(sorted(values, key=lambda v: (abs(v - target), v))[:n])


async def _event_chain(
    symbol: str,
    choose_expiry: Callable[[set[dt.date]], dt.date | None],
    host: str,
    port: int,
    client_id: int,
    strikes: int,
    wait: float,
    grace: float = 1.5,
) -> dict[str, Any]:
    from ib_async import IB, Option, Stock

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
        stock = Stock(symbol, "SMART", "USD")
        if not await ib.qualifyContractsAsync(stock) or not stock.conId:
            raise IBKRSnapshotError(f"{symbol}: no such US stock or ETF at IBKR")
        # Type 2: live during the session, else the last (frozen) quotes. Type 1 returns no option
        # bid/ask outside the session (measured 2026-11-21: -1/-1, no last), so nothing to price.
        ib.reqMarketDataType(2)
        underlying = ib.reqMktData(stock, "", False, False)
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < wait and not (_number(underlying.marketPrice()) or _number(underlying.close)):
            await asyncio.sleep(0.1)
        ib.cancelMktData(stock)
        spot = _number(underlying.marketPrice()) or _number(underlying.close)
        if spot is None:
            detail = f" ({'; '.join(refusals)})" if refusals else ""
            raise IBKRSnapshotError(f"{symbol}: IBKR returned no price within {wait:g}s{detail}")

        params = await ib.reqSecDefOptParamsAsync(symbol, "", "STK", stock.conId)
        chain = next((p for p in params if p.exchange == "SMART"), params[0] if params else None)
        if chain is None:
            raise IBKRSnapshotError(f"{symbol}: IBKR lists no option chain")
        expiries = {dt.datetime.strptime(e, "%Y%m%d").date() for e in chain.expirations}
        result: dict[str, Any] = {
            "current_price": spot,
            "options": [],
            "expirations": sorted(e.isoformat() for e in expiries),
            "freshness": MARKET_DATA_TYPES.get(underlying.marketDataType, f"type {underlying.marketDataType}"),
        }
        expiry = choose_expiry(expiries)
        if expiry is None:
            return result

        # A few strikes around spot, both rights: enough for the ATM straddle, far inside the
        # account's market-data line limit. Not every listed strike exists for every expiry.
        wanted = [
            Option(symbol, expiry.strftime("%Y%m%d"), k, right, "SMART", tradingClass=chain.tradingClass)
            for k in _nearest(chain.strikes, spot, strikes)
            for right in ("C", "P")
        ]
        contracts = [c for c in await ib.qualifyContractsAsync(*wanted) if c and c.conId]
        tickers = [ib.reqMktData(c, "", False, False) for c in contracts]
        start = loop.time()
        # As in _snapshot (#45 review): stop at once with every bid and ask in; once every leg has
        # *some* price (after hours often only a last trade) and the grace period has passed; and
        # at once with nothing to quote. The grace lets live bid/ask beat a stale last (#56 review).
        while tickers and loop.time() - start < wait:
            quoted = all(_number(t.bid) and _number(t.ask) for t in tickers)
            priced = all((_number(t.bid) and _number(t.ask)) or _number(t.last) for t in tickers)
            if _done(priced, quoted, loop.time() - start, grace):
                break
            await asyncio.sleep(0.1)
        for c in contracts:
            ib.cancelMktData(c)
        for c, t in zip(contracts, tickers, strict=True):
            result["options"].append(
                {
                    "option": c.localSymbol,  # OCC form, e.g. "AAPL  261204C00345000"
                    "bid": _number(t.bid),  # IBKR sends -1 for "no quote": None here
                    "ask": _number(t.ask),
                    "last_trade_price": _number(t.last),
                    "freshness": MARKET_DATA_TYPES.get(t.marketDataType, f"type {t.marketDataType}"),
                }
            )
        # Report the stalest data actually used: on a weekend the underlying can come back
        # "live" while every option is "frozen", and the straddle is priced from the options.
        result["freshness"] = max([result["freshness"], *(o["freshness"] for o in result["options"])], key=_staleness)
        return result
    finally:
        ib.disconnect()


def fetch_event_chain(
    symbol: str,
    choose_expiry: Callable[[set[dt.date]], dt.date | None],
    *,
    strikes: int = 5,
    wait: float = 6.0,
    grace: float = 1.5,
) -> dict[str, Any]:
    """The option quotes an earnings straddle needs, from IB Gateway (OPRA), in CBOE's chain shape.

    Returns ``{"current_price", "options": [{"option", "bid", "ask", "last_trade_price"}],
    "expirations", "freshness"}``: what ``analysis.earnings.implied_move`` takes. Only the
    expiry ``choose_expiry`` picks from the listed ones is quoted, at the ``strikes`` strikes
    nearest spot. Choosing is analysis (the event-expiry rule), so the caller passes it in and
    this module stays below ``analysis``. ``freshness`` is ``live`` or ``frozen`` (after the
    close: the last quotes, which can be wide). Raises ``IBKRSnapshotError`` with the reason.
    """
    host, port, client_id = _gateway("")
    return asyncio.run(_event_chain(symbol.upper(), choose_expiry, host, port, client_id, strikes, wait, grace))
