"""Finviz Elite API client.

Uses the FINVIZ_AUTH_TOKEN env var (or `auth_token` arg) to access Elite
endpoints that the free-tier `finvizfinance` package can't reach:

- `grp_export` — sector / industry / country group performance, weekly-friendly
- `quote_export` — full daily history per ticker (better than yfinance for survivability)

Endpoints return CSV. We parse to pandas and add typed accessors.

Every request goes through ``tradekit.data.finviz_http`` (shared rate limit + incremental
backoff). Screens are served from ``get_universe`` — one export of the whole tradeable universe,
cached for ``finviz_cache_ttl_minutes`` — and filtered locally, so ``scan``, ``second-day`` and
``premarket_rvol.py`` running in the same window cost one Finviz request instead of one each.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from tradekit.data.finviz_http import finviz_get, redact

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

GROUP_TYPES = ("sector", "industry", "country")

# Column codes per the Elite Groups export spec.
# 0=No, 1=Name, 2=MktCap, 14=FloatShort, 15=PerfWeek, 16=PerfMonth,
# 17=PerfQuarter, 18=PerfHalfYear, 19=PerfYear, 20=PerfYTD,
# 21=AnalystRecom, 22=AvgVolume, 23=RelVolume, 24=Change, 25=Volume, 26=Stocks
DEFAULT_GROUP_COLS = "0,1,15,16,17,18,19,20,23,24,26"

GROUP_COL_RENAME = {
    "Performance (Week)": "perf_w",
    "Performance (Month)": "perf_m",
    "Performance (Quarter)": "perf_q",
    "Performance (Half Year)": "perf_h",
    "Performance (Year)": "perf_y",
    "Performance (Year To Date)": "perf_ytd",
    "Relative Volume": "rvol",
    "Change": "change",
    "Stocks": "stocks",
    "Name": "name",
    "No.": "no",
}


@dataclass
class FinvizEliteConfig:
    auth_token: str
    base_elite: str = "https://elite.finviz.com"
    timeout: int = 15

    @classmethod
    def from_env(cls) -> "FinvizEliteConfig | None":
        tok = os.getenv("FINVIZ_AUTH_TOKEN")
        if not tok:
            # Fall back to the shared .env chain. This used to read a hardcoded
            # ~/Projects/falcon/.env, which only resolved on one machine and
            # never under WSL, where $HOME is a different filesystem entirely.
            from pathlib import Path

            from tradekit.config import env_file_candidates

            for p in [*env_file_candidates(), Path.home() / ".env"]:
                if p.exists():
                    for line in p.read_text().splitlines():
                        if line.strip().startswith("FINVIZ_AUTH_TOKEN"):
                            tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
                if tok:
                    break
        if not tok:
            return None
        return cls(auth_token=tok)


class FinvizEliteProvider:
    """Finviz Elite API client — groups + historical quotes."""

    def __init__(self, config: FinvizEliteConfig | None = None):
        self.config = config or FinvizEliteConfig.from_env()
        if not self.config:
            raise RuntimeError("FINVIZ_AUTH_TOKEN not set in env or any .env on the shared load chain")

    def _pct(self, s: object) -> float:
        if not isinstance(s, str):
            return float(s) if s is not None else 0.0
        s = s.strip().rstrip("%")
        if not s or s == "-":
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0

    def get_group(self, group_type: str = "sector", cols: str = DEFAULT_GROUP_COLS) -> pd.DataFrame:
        """Fetch a group performance table.

        Args:
            group_type: "sector", "industry", or "country".
            cols: Comma-separated Finviz column codes.

        Returns:
            DataFrame with normalized column names: name, perf_w/m/q/h/y/ytd as floats,
            rvol, change, stocks (int).
        """
        if group_type not in GROUP_TYPES:
            raise ValueError(f"group_type must be one of {GROUP_TYPES}")

        url = f"{self.config.base_elite}/grp_export?g={group_type}&v=152&c={cols}&auth={self.config.auth_token}"
        r = finviz_get(url, expect_csv=True, timeout=self.config.timeout)

        df = pd.read_csv(io.StringIO(r.text))
        df = df.rename(columns=GROUP_COL_RENAME)

        # Coerce percentage columns to floats
        for col in ["perf_w", "perf_m", "perf_q", "perf_h", "perf_y", "perf_ytd", "change"]:
            if col in df.columns:
                df[col] = df[col].apply(self._pct)
        if "rvol" in df.columns:
            df["rvol"] = pd.to_numeric(df["rvol"], errors="coerce").fillna(0.0)
        if "stocks" in df.columns:
            df["stocks"] = pd.to_numeric(df["stocks"], errors="coerce").fillna(0).astype(int)

        return df

    def get_quote_history(self, ticker: str) -> pd.DataFrame:
        """Fetch full daily OHLCV history for a ticker via quote_export.

        Returns columns: date (datetime), open, high, low, close, volume.
        Note: `p` parameter is cosmetic for this endpoint — always returns full history.
        """
        url = f"{self.config.base_elite}/quote_export?t={ticker}&p=d&auth={self.config.auth_token}"
        r = finviz_get(url, expect_csv=True, timeout=self.config.timeout)

        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.lower() for c in df.columns]
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    # Finviz Elite screener-export column codes. 59-62 re-verified 2026-10-06 against live
    # export headers: the earlier table had rsi/change_open/gap shifted by one (60/61/62),
    # so those keys silently returned Change-from-Open / Gap / Analyst Recom.
    # Units in this export: Average Volume is in thousands, Market Cap in millions.
    SCREENER_COLS = {
        "ticker": 1,
        "company": 2,
        "sector": 3,
        "industry": 4,
        "country": 5,
        "market_cap": 6,
        "pe": 7,
        "perf_w": 42,
        "perf_m": 43,
        "perf_q": 44,
        "perf_h": 45,
        "perf_y": 46,
        "perf_ytd": 47,
        "beta": 48,
        "atr": 49,
        "vol_week": 50,
        "rsi": 59,
        "change_open": 60,
        "gap": 61,
        "analyst_recom": 62,
        "avg_vol": 63,
        "rvol": 64,
        "price": 65,
        "change": 66,
        "volume": 67,
        "earnings": 68,
        "target": 69,
        "ipo": 70,
        "ah_close": 71,
        "ah_change": 72,
    }

    def get_quotes(self, tickers: list[str], cols: list[str] | None = None) -> pd.DataFrame:
        """Batch fetch live quote + fundamental + AH data for one or more tickers.

        Default col set covers the most useful fields for trade prep including AH.
        """
        if not tickers:
            return pd.DataFrame()
        cached = self._quotes_from_universe(tickers, cols or self.DEFAULT_QUOTE_COLS)
        if cached is not None:
            return cached
        cols = cols or self.DEFAULT_QUOTE_COLS
        col_codes = ",".join(str(self.SCREENER_COLS[c]) for c in cols if c in self.SCREENER_COLS)
        ticker_str = ",".join(t.upper() for t in tickers)
        url = f"{self.config.base_elite}/export.ashx?v=152&t={ticker_str}&c={col_codes}&auth={self.config.auth_token}"
        r = finviz_get(url, expect_csv=True, timeout=self.config.timeout)

        return self._coerce_pct(pd.read_csv(io.StringIO(r.text)))

    def _coerce_pct(self, df: pd.DataFrame) -> pd.DataFrame:
        """Turn Finviz '4.51%' string columns into floats (in place) and return the frame."""
        for col in df.columns:
            if not pd.api.types.is_numeric_dtype(df[col]):
                sample = df[col].dropna().astype(str).head(3).tolist()
                if sample and any(s.endswith("%") for s in sample):
                    df[col] = df[col].apply(self._pct)
        return df

    # get_quotes' default column set, named so the universe cache can be checked against it.
    DEFAULT_QUOTE_COLS = [
        "ticker",
        "company",
        "sector",
        "industry",
        "price",
        "change",
        "volume",
        "ah_close",
        "ah_change",
        "perf_w",
        "perf_m",
        "perf_q",
        "perf_ytd",
        "rsi",
        "atr",
        "rvol",
        "earnings",
    ]

    # Superset fetched by get_universe: everything the screens and get_quotes' defaults read.
    UNIVERSE_COLS = [
        *DEFAULT_QUOTE_COLS,
        "country",
        "market_cap",
        "change_open",
        "gap",
        "avg_vol",
    ]

    # Price over $1 — the broadest net that still drops sub-dollar noise; narrower screens
    # (min price, volume, change) are applied locally.
    UNIVERSE_FILTERS = "sh_price_o1"

    # ----- universe superset: one export, filtered locally -----

    def _universe_paths(self, filters: str, cols: list[str]) -> tuple[Path, Path]:
        from tradekit.paths import state_dir

        key = hashlib.sha1(f"{filters}|{','.join(cols)}".encode()).hexdigest()[:12]
        base = state_dir() / "finviz_universe"
        return base / f"{key}.csv", base / f"{key}.json"

    @staticmethod
    def _ttl_seconds(ttl_minutes: float | None) -> float:
        if ttl_minutes is None:
            from tradekit.config import DataSettings

            ttl_minutes = DataSettings().finviz_cache_ttl_minutes
        return float(ttl_minutes) * 60.0

    def get_universe(
        self,
        filters: str | None = None,
        cols: list[str] | None = None,
        ttl_minutes: float | None = None,
        refresh: bool = False,
    ) -> pd.DataFrame:
        """One Elite export of the whole filtered universe, cached on disk for ``ttl_minutes``.

        Every screen (top gainers, unusual volume, most active, previous movers) is a local
        filter over this frame, so any number of screens in the TTL cost one Finviz request.
        Concurrent processes serialize on a lock file: the second waits and reads the first's
        cache instead of fetching again.

        Raises ``FinvizThrottled`` if Finviz keeps refusing and ``ValueError`` if the export
        comes back without a Ticker column — never an empty frame standing in for "no data".
        """
        filters = filters or self.UNIVERSE_FILTERS
        cols = cols or self.UNIVERSE_COLS
        csv_path, meta_path = self._universe_paths(filters, cols)
        ttl = self._ttl_seconds(ttl_minutes)
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        lock_fh = open(csv_path.with_suffix(".lock"), "a+")
        try:
            if fcntl is not None:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            if not refresh and csv_path.exists() and time.time() - csv_path.stat().st_mtime < ttl:
                logger.info("Finviz universe from cache (%s)", csv_path.name)
                return pd.read_csv(csv_path)

            codes = ",".join(str(self.SCREENER_COLS[c]) for c in cols if c in self.SCREENER_COLS)
            url = f"{self.config.base_elite}/export.ashx?v=152&f={filters}&c={codes}&auth={self.config.auth_token}"
            logger.info("Fetching Finviz universe: %s", redact(url))
            r = finviz_get(url, expect_csv=True, timeout=max(self.config.timeout, 60))
            df = pd.read_csv(io.StringIO(r.text))
            if "Ticker" not in df.columns:
                raise ValueError(f"Finviz universe export has no Ticker column (got {list(df.columns)[:6]})")

            tmp = csv_path.with_suffix(".tmp")
            df.to_csv(tmp, index=False)
            tmp.replace(csv_path)
            meta_path.write_text(
                json.dumps({"filters": filters, "cols": cols, "rows": len(df), "fetched": time.time()})
            )
            logger.info("Finviz universe: %d rows cached", len(df))
            return df
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()

    def _quotes_from_universe(self, tickers: list[str], cols: list[str]) -> pd.DataFrame | None:
        """Serve get_quotes from a fresh universe cache, or None if it can't answer completely."""
        csv_path, meta_path = self._universe_paths(self.UNIVERSE_FILTERS, self.UNIVERSE_COLS)
        try:
            if time.time() - csv_path.stat().st_mtime >= self._ttl_seconds(None):
                return None
            meta = json.loads(meta_path.read_text())
        except OSError, ValueError:
            return None
        if not set(cols) <= set(meta.get("cols", [])):
            return None
        df = pd.read_csv(csv_path)
        want = {t.upper() for t in tickers}
        out = df[df["Ticker"].astype(str).str.upper().isin(want)]
        if len(out) < len(want):
            return None  # a name missing from the universe (e.g. sub-$1) — ask Finviz directly
        logger.info("get_quotes: %d tickers served from the universe cache", len(out))
        return self._coerce_pct(out.reset_index(drop=True))

    # ----- DataProvider Protocol implementations -----

    def get_quote(self, ticker: str) -> dict:
        """Single-ticker live quote + AH (DataProvider Protocol method)."""
        df = self.get_quotes([ticker])
        if df.empty:
            return {}
        row = df.iloc[0].to_dict()
        # Normalize for tradekit's expected schema
        return {
            "ticker": row.get("Ticker"),
            "company": row.get("Company"),
            "price": row.get("Price"),
            "change": row.get("Change"),
            "volume": row.get("Volume"),
            "industry": row.get("Industry"),
            "sector": row.get("Sector"),
            "ah_price": row.get("After-Hours Close"),
            "ah_change": row.get("After-Hours Change"),
            "perf_week": row.get("Performance (Week)"),
            "perf_month": row.get("Performance (Month)"),
            "atr": row.get("Average True Range"),
            "rsi": row.get("Relative Strength Index (14)"),
        }

    def get_history(self, ticker: str, period: str = "3mo", interval: str = "1d") -> pd.DataFrame:
        """Daily OHLCV history (DataProvider Protocol method).

        `period` accepted for interface compatibility — endpoint returns full history,
        we slice to the requested window.
        """
        df = self.get_quote_history(ticker)
        if df.empty:
            return df
        # Slice by period
        period_days = {"1mo": 22, "3mo": 66, "6mo": 132, "1y": 252, "2y": 504, "5y": 1260, "max": 99999}.get(period, 66)
        df = df.tail(period_days).copy()
        # Match yfinance's capitalized column names so existing consumers work
        df = df.rename(
            columns={"date": "Date", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
        )
        df = df.set_index("Date")
        return df

    def get_premarket(self, ticker: str) -> dict:
        """Return After-Hours data — Finviz exposes AH but not pre-market specifically."""
        q = self.get_quote(ticker)
        return {
            "ticker": ticker,
            "ah_price": q.get("ah_price"),
            "ah_change_pct": q.get("ah_change"),
            "regular_close": q.get("price"),
            "note": "Finviz Elite exposes After-Hours, not Pre-Market. Treat ah_price as session-end snapshot.",
        }


# ----- local screens over a get_universe() frame -----


def _numeric(series: pd.Series) -> pd.Series:
    """Finviz cells arrive as floats or as '4.51%' / '-' strings; return floats (NaN for blanks)."""
    if not pd.api.types.is_numeric_dtype(series):
        series = series.astype(str).str.strip().str.rstrip("%").replace({"-": None, "": None, "nan": None})
    return pd.to_numeric(series, errors="coerce")


def screen_universe(
    universe: pd.DataFrame,
    *,
    min_price: float | None = None,
    max_price: float | None = None,
    min_volume: float | None = None,
    min_avg_volume: float | None = None,
    min_rvol: float | None = None,
    min_change: float | None = None,
    max_change: float | None = None,
    sort_by: str = "Change",
    ascending: bool = False,
    limit: int | None = None,
) -> pd.DataFrame:
    """Filter and rank the cached universe locally — no Finviz request.

    Column names are Finviz's export headers (``Price``, ``Change``, ``Volume``,
    ``Relative Volume``). Rows missing a value a filter needs are dropped, not defaulted to 0.
    """
    df = universe.copy()
    for col in ("Price", "Change", "Volume", "Relative Volume"):
        if col in df.columns:
            df[col] = _numeric(df[col])
    if "Average Volume" in df.columns:
        df["_avg_vol_shares"] = _numeric(df["Average Volume"]) * 1000  # export reports thousands
    mask = pd.Series(True, index=df.index)
    for col, lo, hi in (
        ("Price", min_price, max_price),
        ("Volume", min_volume, None),
        ("_avg_vol_shares", min_avg_volume, None),
        ("Relative Volume", min_rvol, None),
        ("Change", min_change, max_change),
    ):
        if lo is None and hi is None:
            continue
        if col not in df.columns:
            name = "Average Volume" if col == "_avg_vol_shares" else col
            raise KeyError(f"universe has no {name!r} column; refetch with it in UNIVERSE_COLS")
        if lo is not None:
            mask &= df[col] >= lo
        if hi is not None:
            mask &= df[col] <= hi
    df = df[mask]
    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=ascending, na_position="last")
    df = df.drop(columns=["_avg_vol_shares"], errors="ignore").reset_index(drop=True)
    return df.head(limit) if limit else df


def top_gainers(
    universe: pd.DataFrame,
    min_price: float = 5.0,
    limit: int | None = 50,
    min_avg_volume: float | None = None,
) -> pd.DataFrame:
    """Local equivalent of Finviz's Top Gainers signal: positive change, highest first.

    ``min_avg_volume`` (shares) drops illiquid prints — off-hours the export's Change can be a
    4-share trade, which would otherwise top the list.
    """
    return screen_universe(
        universe,
        min_price=min_price,
        min_avg_volume=min_avg_volume,
        min_change=0.0001,
        sort_by="Change",
        limit=limit,
    )


def top_losers(universe: pd.DataFrame, min_price: float = 5.0, limit: int | None = 50) -> pd.DataFrame:
    """Negative change, biggest drop first."""
    return screen_universe(
        universe, min_price=min_price, max_change=-0.0001, sort_by="Change", ascending=True, limit=limit
    )


def unusual_volume(
    universe: pd.DataFrame, min_price: float = 5.0, min_rvol: float = 2.0, limit: int | None = 50
) -> pd.DataFrame:
    """Relative volume at or above ``min_rvol``, highest first."""
    return screen_universe(universe, min_price=min_price, min_rvol=min_rvol, sort_by="Relative Volume", limit=limit)


def most_active(universe: pd.DataFrame, min_price: float = 5.0, limit: int | None = 50) -> pd.DataFrame:
    """Highest share volume."""
    return screen_universe(universe, min_price=min_price, sort_by="Volume", limit=limit)


def diff_groups(
    today: dict, prior: dict, group_type: str = "sector", metric: str = "perf_w", top_n: int = 10
) -> list[dict]:
    """Compute week-over-week deltas between two group snapshots.

    Returns list of {name, today, prior, delta} sorted by delta (improvers first).
    """
    today_idx = {r["name"]: r for r in today.get(group_type, [])}
    prior_idx = {r["name"]: r for r in prior.get(group_type, [])}
    deltas = []
    for name, t in today_idx.items():
        p = prior_idx.get(name)
        if not p:
            continue
        deltas.append(
            {
                "name": name,
                "today": t.get(metric),
                "prior": p.get(metric),
                "delta": t.get(metric, 0) - p.get(metric, 0),
            }
        )
    deltas.sort(key=lambda x: x["delta"], reverse=True)
    return deltas


def compute_rs_spread(quotes_df: pd.DataFrame, groups_industry: list[dict]) -> pd.DataFrame:
    """Compute relative-strength spread = ticker_perf_w - industry_perf_w.

    Args:
        quotes_df: output of FinvizEliteProvider.get_quotes — must include
                   'Ticker', 'Industry', 'Performance (Week)' columns.
        groups_industry: list of dicts from a saved groups snapshot's "industry" key.

    Returns DataFrame with columns: ticker, industry, ticker_w, industry_w, rs_spread.
    """
    industry_w = {r["name"]: r["perf_w"] for r in groups_industry}
    rows = []
    for _, q in quotes_df.iterrows():
        tk = q.get("Ticker")
        ind = q.get("Industry")
        tw = q.get("Performance (Week)")
        if tk is None or ind is None or tw is None:
            continue
        iw = industry_w.get(ind)
        if iw is None:
            continue
        rows.append(
            {
                "ticker": tk,
                "industry": ind,
                "ticker_w": float(tw),
                "industry_w": float(iw),
                "rs_spread": float(tw) - float(iw),
            }
        )
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("rs_spread", key=lambda s: s.abs(), ascending=False)
    return out
