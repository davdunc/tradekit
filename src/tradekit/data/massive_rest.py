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
#: Hosts a ``next_url`` may point at. The API key is appended to it, so never anywhere else.
API_HOSTS = ("api.polygon.io", "api.massive.com")
#: Cash-index options are requested under Massive's ``I:`` prefix: the bare root returns no greeks.
INDEX_UNDERLYINGS = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"}


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
        return self._get_url(f"{BASE}{path}", params)

    def _get_url(self, url: str, params: dict | None = None) -> dict:
        from urllib.parse import urlparse

        if urlparse(url).hostname not in API_HOSTS:  # a next_url elsewhere would get our key
            raise MassiveError("bad-host", url)
        self.limiter.acquire()
        self.requests_made += 1
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

    def option_chain(self, underlying: str, *, expiration_lte: dt.date | None = None, max_pages: int = 200) -> dict:
        """Every option contract on ``underlying`` expiring by ``expiration_lte``, from one paged snapshot.

        ``GET /v3/snapshot/options/{underlying}`` at 250 contracts a page, following ``next_url``.
        Needs an options plan with snapshots (Starter and up; real-time on Advanced). A 403 means the
        key isn't entitled; it is logged and raised like any other, never retried.
        """
        symbol = underlying.upper()
        path_symbol = f"I:{symbol}" if symbol in INDEX_UNDERLYINGS else symbol
        params: dict = {"limit": 250}
        if expiration_lte is not None:
            params["expiration_date.lte"] = expiration_lte.isoformat()
        page = self._get(f"/v3/snapshot/options/{path_symbol}", params)
        results = list(page.get("results") or [])
        pages = 1
        while page.get("next_url"):
            if pages >= max_pages:
                raise MassiveError("too-many-pages", f"{path_symbol}: more than {max_pages} pages; narrow --max-dte")
            page = self._get_url(page["next_url"])
            results.extend(page.get("results") or [])
            pages += 1
        chain = chain_from_snapshot(results)
        chain["pages"] = pages
        return chain


# ----- options chain snapshot, for tradekit gex (#55: CBOE blocks automated access) -----


def chain_from_snapshot(results: list[dict]) -> dict:
    """Massive option-chain snapshot results in the chain shape ``analysis.gex.compute_gex`` reads.

    ``{"current_price", "options": [{"option", "open_interest", "iv"}], "freshness", "contracts"}``.
    ``option`` is the OCC symbol (Massive's ``details.ticker`` without its ``O:`` prefix). A
    contract with no implied volatility (Massive omits greeks/IV for some deep in-the-money
    contracts) is kept with ``iv`` 0, which ``compute_gex`` skips. ``freshness`` is the
    underlying's ``timeframe`` (``delayed`` or ``real-time``), as Massive reports it.
    """
    spot = freshness = None
    options = []
    for r in results:
        asset = r.get("underlying_asset") or {}
        if spot is None and asset.get("price"):
            spot = asset["price"]
            freshness = str(asset.get("timeframe") or "unknown").lower()
        ticker = (r.get("details") or {}).get("ticker") or ""
        options.append(
            {
                "option": ticker.removeprefix("O:"),
                "open_interest": r.get("open_interest") or 0,
                "iv": r.get("implied_volatility") or 0,
            }
        )
    return {"current_price": spot, "options": options, "freshness": freshness or "unknown", "contracts": len(options)}
