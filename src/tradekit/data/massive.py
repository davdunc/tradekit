"""Massive data provider (``--source massive``), on Massive's REST API.

It used to talk to the ``mcp_massive`` MCP server through a ``uvx`` subprocess per call. That
never worked against the pinned server: ``mcp_massive`` 0.6+ returns CSV and this module parsed
JSON, so every method raised (#52). It now uses :class:`~tradekit.data.massive_rest.MassiveREST`
(key and host from config, self rate-limited, a 403 logged and never retried). The ``mcp``
dependency is gone with it.

Shapes follow the ``DataProvider`` contract (``data/base.py``), including the premarket gap
contract (#51): no premarket session means ``gap_pct`` and ``pre_price`` are ``None``, never 0.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

import pandas as pd

from tradekit.config import now_et
from tradekit.data.massive_rest import NY, MassiveREST

logger = logging.getLogger(__name__)

#: yfinance-style period strings, as the other providers take them.
_PERIOD_DAYS = {"1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
#: yfinance-style interval strings -> Massive (multiplier, timespan).
_INTERVAL = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h": (1, "hour"),
    "1d": (1, "day"),
    "1wk": (1, "week"),
    "1mo": (1, "month"),
}
#: Sessions in the average-volume baseline (the snapshot carries no average).
AVG_VOLUME_SESSIONS = 20
#: Calendar days fetched for it: 20 sessions is ~28 days, and this leaves room for holiday weeks.
AVG_VOLUME_LOOKBACK_DAYS = 45
PREMARKET_OPEN = dt.time(4, 0)
REGULAR_OPEN = dt.time(9, 30)


def _bar_time(bar: dict) -> dt.datetime | None:
    t = bar.get("t")
    return dt.datetime.fromtimestamp(t / 1000, NY) if t else None


def in_premarket(minute_bar: dict, now: dt.datetime) -> bool:
    """The latest minute bar falls in *today's* 04:00-09:30 ET window: a premarket session is on."""
    when = _bar_time(minute_bar)
    now = now.astimezone(NY)
    return when is not None and when.date() == now.date() and PREMARKET_OPEN <= when.time() < REGULAR_OPEN


class MassiveProvider:
    """``DataProvider`` on Massive's REST API. ``rest`` and ``clock`` are injectable for tests."""

    def __init__(self, rest: MassiveREST | None = None, clock: Callable[[], dt.datetime] = now_et):
        self._rest = rest if rest is not None else MassiveREST()  # raises MassiveError without a key
        self._clock = clock

    def _avg_volume(self, ticker: str) -> float:
        today = self._clock().date()
        bars = self._rest.daily_bars(ticker, today - dt.timedelta(days=AVG_VOLUME_LOOKBACK_DAYS), today)
        done = bars[bars["date"] < today] if len(bars) else bars  # completed sessions only
        volumes = done["volume"].tail(AVG_VOLUME_SESSIONS)
        return float(volumes.mean()) if len(volumes) else 0.0

    def get_quote(self, ticker: str) -> dict:
        """Regular-session price (the day bar), the previous close, and a 20-session average volume."""
        snap = self._rest.stock_snapshot(ticker)
        day, minute, prev = snap.get("day") or {}, snap.get("min") or {}, snap.get("prevDay") or {}
        updated = snap.get("updated")
        return {
            "ticker": ticker.upper(),
            "price": day.get("c") or minute.get("c") or prev.get("c") or 0,
            "prev_close": prev.get("c") or 0,
            "open": day.get("o") or 0,
            "high": day.get("h") or 0,
            "low": day.get("l") or 0,
            "volume": day.get("v") or 0,
            "avg_volume": self._avg_volume(ticker),
            # Not in the snapshot (the reference endpoint has them); 0 as before, not invented.
            "market_cap": 0,
            "float_shares": 0,
            "name": ticker.upper(),
            "market_time": updated / 1e9 if updated else None,  # epoch seconds, as Yahoo's quote carries
        }

    def get_history(self, ticker: str, period: str = "3mo", interval: str = "1d") -> pd.DataFrame:
        """OHLCV over ``period`` at ``interval`` (yfinance-style), indexed by New York time."""
        days = _PERIOD_DAYS.get(period, 90)
        multiplier, timespan = _INTERVAL.get(interval, (1, "day"))
        end = self._clock().date()
        df = self._rest.aggs(ticker, multiplier, timespan, end - dt.timedelta(days=days), end)
        if df.empty:
            logger.warning("No history data for %s from Massive", ticker)
        return df

    def get_premarket(self, ticker: str) -> dict:
        """Premarket per the #51 contract: ``has_premarket`` only while today's premarket is trading."""
        snap = self._rest.stock_snapshot(ticker)
        minute, prev = snap.get("min") or {}, snap.get("prevDay") or {}
        prev_close = prev.get("c") or 0
        has_premarket = in_premarket(minute, self._clock()) and bool(minute.get("c"))
        pre_price = minute.get("c") if has_premarket else None
        gap = (pre_price - prev_close) / prev_close * 100 if pre_price and prev_close else None
        return {
            "ticker": ticker.upper(),
            "has_premarket": has_premarket,
            "pre_price": pre_price,
            "prev_close": prev_close,
            "gap_pct": round(gap, 2) if gap is not None else None,
            "pre_volume": (minute.get("av") or 0) if has_premarket else 0,
            "avg_volume": 0,
            "name": ticker.upper(),
            "market_cap": 0,
            "float_shares": 0,
        }

    def get_multiple_quotes(self, tickers: list[str]) -> list[dict]:
        results = []
        for ticker in tickers:
            try:
                results.append(self.get_quote(ticker))
            except Exception as e:  # noqa: BLE001 - one ticker failing must not sink the batch
                logger.warning("Failed to fetch %s from Massive: %s", ticker, e)
        return results

    def get_multiple_premarket(self, tickers: list[str]) -> list[dict]:
        results = []
        for ticker in tickers:
            try:
                data = self.get_premarket(ticker)
                if data.get("has_premarket"):  # as YahooProvider does: no premarket, no row
                    results.append(data)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to fetch pre-market for %s from Massive: %s", ticker, e)
        return results
