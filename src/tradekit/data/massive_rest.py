"""Minimal Massive (Polygon) REST client for bulk reads: daily bars and grouped daily aggregates.

Massive is the first-choice data source (LifeOS rule, 2026-10-06). Stocks are unlimited on our plan, but
access is protected deliberately:

- self-capped at 10 requests/second (shared limiter file, all local processes);
- a 403 is recorded to the api-errors log and raised, never retried (403 = not entitled, or a bad key);
- 429 and 5xx are raised too. Callers decide; nothing here loops against the feed.

Daily-bar timestamps (``t``) are epoch ms at **00:00 America/New_York**. Converting them in UTC or
naive local time can mislabel the session, so dates are taken in New York time.
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from tradekit.data import api_errors
from tradekit.data.finviz_http import RateLimiter, redact

BASE = "https://api.polygon.io"
NY = ZoneInfo("America/New_York")


class MassiveError(RuntimeError):
    """A Massive request failed; the status and redacted URL are in the message."""

    def __init__(self, status: int | str, url: str):
        super().__init__(f"Massive {status}: {redact_key(url)}")
        self.status = status


def redact_key(url: str) -> str:
    import re

    return re.sub(r"(apiKey=)[^&]+", r"\1***", redact(url))


def _api_key() -> str:
    from tradekit.config import DataSettings

    return DataSettings().polygon_api_key or os.environ.get("POLYGON_API_KEY", "")


class MassiveREST:
    def __init__(
        self, api_key: str | None = None, limiter: RateLimiter | None = None, timeout: float = 30, app: str = "tradekit"
    ):
        self.api_key = api_key if api_key is not None else _api_key()
        if not self.api_key:
            raise MassiveError("no-key", "POLYGON_API_KEY not set")
        if limiter is None:
            from tradekit.paths import state_dir

            limiter = RateLimiter(max_requests=10, window=1.0, path=state_dir() / "massive_ratelimit.json")
        self.limiter = limiter
        self.timeout = timeout
        self.app = app
        self.requests_made = 0

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.limiter.acquire()
        self.requests_made += 1
        url = f"{BASE}{path}"
        try:
            r = requests.get(url, params={**(params or {}), "apiKey": self.api_key}, timeout=self.timeout)
        except requests.RequestException as e:
            raise MassiveError(type(e).__name__, redact_key(url)) from e
        if r.status_code == 403:
            api_errors.record("massive", r.url, 403, detail=r.reason or "", app=self.app)
        if r.status_code != 200:
            raise MassiveError(r.status_code, r.url)
        return r.json()

    def daily_bars(self, ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        j = self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}", {"adjusted": "true", "sort": "asc", "limit": 50000}
        )
        rows = j.get("results") or []
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(NY).dt.date
        return df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[
            ["date", "open", "high", "low", "close", "volume"]
        ]

    def grouped_daily(self, day: dt.date) -> pd.DataFrame:
        j = self._get(f"/v2/aggs/grouped/locale/us/market/stocks/{day}", {"adjusted": "true"})
        return pd.DataFrame(j.get("results") or [])
