"""Minimal Massive REST client: daily bars, grouped daily aggregates, stock and option-chain snapshots.

Massive is the vendor formerly called Polygon. The key and base URL come only from
``config.DataSettings`` (``massive_api_key``, ``massive_api_base``); deprecated names such as
``POLYGON_API_KEY`` are resolved there too (#58). Nothing here names a host or a variable.

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
import logging
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from tradekit.data import api_errors
from tradekit.data.finviz_http import RateLimiter, redact

NY = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)
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


class MassiveREST:
    def __init__(
        self,
        api_key: str | None = None,
        limiter: RateLimiter | None = None,
        timeout: float = 30,
        app: str = "tradekit",
        base_url: str | None = None,
    ):
        from tradekit.config import DataSettings, env_var

        data = DataSettings() if api_key is None or base_url is None else None
        self.api_key = api_key if api_key is not None else data.massive_api_key
        self.base_url = (base_url if base_url is not None else data.massive_api_base).rstrip("/")
        if not self.api_key:
            raise MassiveError("no-key", f"{env_var('massive_api_key')} not set")
        if limiter is None:
            from tradekit.paths import state_dir

            limiter = RateLimiter(max_requests=10, window=1.0, path=state_dir() / "massive_ratelimit.json")
        self.limiter = limiter
        self.timeout = timeout
        self.app = app
        self.requests_made = 0

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._get_url(f"{self.base_url}{path}", params)

    def _get_url(self, url: str, params: dict | None = None) -> dict:
        # The key is appended to every request, so only ever send it to the configured host.
        # Massive returns next_url on the host it was asked (checked 2026-11-22).
        if urlparse(url).hostname != urlparse(self.base_url).hostname:
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
        if chain["current_price"] is None:
            # Some options plans return underlying_asset as {"ticker"} only (seen 2026-11-22):
            # take spot from the stock snapshot, and say where it came from.
            if symbol in INDEX_UNDERLYINGS:
                raise MassiveError(
                    "no-spot",
                    f"{path_symbol}: the options snapshot has no index level, and index spot isn't supported yet",
                )
            price, when = self.stock_price(symbol)
            chain["current_price"] = price
            chain["spot_asof"] = when
            chain["spot_source"] = "stock snapshot"
        else:
            chain["spot_source"] = "options snapshot"
        return chain

    def stock_price(self, ticker: str) -> tuple[float, dt.datetime | None]:
        """Latest price from the stock snapshot, and when it was taken.

        The latest minute bar's close, else the day's close, else the previous close. Massive's
        day bar includes extended hours, so after the close this can differ from the regular-session
        close. The timestamp says which moment it is.
        """
        snap = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}").get("ticker") or {}
        for bar in ("min", "day", "prevDay"):
            price = (snap.get(bar) or {}).get("c")
            if price:
                updated = snap.get("updated")
                return float(price), dt.datetime.fromtimestamp(updated / 1e9, NY) if updated else None
        raise MassiveError("no-spot", f"{ticker}: the stock snapshot has no price")


# ----- options chain snapshot, for tradekit gex (#55: CBOE blocks automated access) -----


def chain_from_snapshot(results: list[dict]) -> dict:
    """Massive option-chain snapshot results in the chain shape ``analysis.gex.compute_gex`` reads.

    ``{"current_price", "options": [{"option", "open_interest", "iv"}], "freshness", "asof",
    "contracts"}``. ``option`` is the OCC symbol (Massive's ``details.ticker`` without its ``O:``
    prefix). A contract with no implied volatility (Massive omits greeks/IV for some deep
    in-the-money contracts) is kept with ``iv`` 0, which ``compute_gex`` skips.

    ``current_price`` and ``freshness`` come from ``underlying_asset`` (``price``, ``timeframe``)
    when the plan returns them. Some don't: ``current_price`` is then None, for the caller to fill,
    and ``freshness`` is ``unknown``. ``asof`` is the latest ``day.last_updated`` across contracts,
    so the data's age is visible either way.
    """
    spot = freshness = None
    latest_ns = 0
    options = []
    for r in results:
        asset = r.get("underlying_asset") or {}
        if spot is None and asset.get("price"):
            spot = asset["price"]
            freshness = str(asset.get("timeframe") or "unknown").lower()
        latest_ns = max(latest_ns, int((r.get("day") or {}).get("last_updated") or 0))
        ticker = (r.get("details") or {}).get("ticker") or ""
        options.append(
            {
                "option": ticker.removeprefix("O:"),
                "open_interest": r.get("open_interest") or 0,
                "iv": r.get("implied_volatility") or 0,
            }
        )
    return {
        "current_price": spot,
        "options": options,
        "freshness": freshness or "unknown",
        "asof": dt.datetime.fromtimestamp(latest_ns / 1e9, NY) if latest_ns else None,
        "contracts": len(options),
    }
