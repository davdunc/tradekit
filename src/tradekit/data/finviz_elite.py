"""Finviz Elite API client.

Uses the FINVIZ_AUTH_TOKEN env var (or `auth_token` arg) to access Elite
endpoints that the free-tier `finvizfinance` package can't reach:

- `grp_export` — sector / industry / country group performance, weekly-friendly
- `quote_export` — full daily history per ticker (better than yfinance for survivability)

Endpoints return CSV. We parse to pandas and add typed accessors.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass

import pandas as pd
import requests

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
            # Fallback: read ~/Projects/falcon/.env (where it actually lives)
            from pathlib import Path
            for p in [Path.home() / "Projects/falcon/.env", Path.home() / ".env"]:
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
            raise RuntimeError("FINVIZ_AUTH_TOKEN not set in env or ~/Projects/falcon/.env")

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
        r = requests.get(url, timeout=self.config.timeout)
        r.raise_for_status()

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
        r = requests.get(url, timeout=self.config.timeout)
        r.raise_for_status()

        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.lower() for c in df.columns]
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    # Verified Finviz Elite screener-export column codes
    SCREENER_COLS = {
        "ticker": 1, "company": 2, "sector": 3, "industry": 4, "country": 5,
        "market_cap": 6, "pe": 7,
        "perf_w": 42, "perf_m": 43, "perf_q": 44, "perf_h": 45, "perf_y": 46, "perf_ytd": 47,
        "beta": 48, "atr": 49, "vol_week": 50,
        "rsi": 60, "change_open": 61, "gap": 62, "avg_vol": 63, "rvol": 64,
        "price": 65, "change": 66, "volume": 67, "earnings": 68, "target": 69, "ipo": 70,
        "ah_close": 71, "ah_change": 72,
    }

    def get_quotes(self, tickers: list[str], cols: list[str] | None = None) -> pd.DataFrame:
        """Batch fetch live quote + fundamental + AH data for one or more tickers.

        Default col set covers the most useful fields for trade prep including AH.
        """
        if not tickers:
            return pd.DataFrame()
        cols = cols or [
            "ticker", "company", "sector", "industry", "price", "change",
            "volume", "ah_close", "ah_change",
            "perf_w", "perf_m", "perf_q", "perf_ytd",
            "rsi", "atr", "rvol", "earnings",
        ]
        col_codes = ",".join(str(self.SCREENER_COLS[c]) for c in cols if c in self.SCREENER_COLS)
        ticker_str = ",".join(t.upper() for t in tickers)
        url = (
            f"{self.config.base_elite}/export.ashx"
            f"?v=152&t={ticker_str}&c={col_codes}&auth={self.config.auth_token}"
        )
        r = requests.get(url, timeout=self.config.timeout)
        r.raise_for_status()

        df = pd.read_csv(io.StringIO(r.text))
        # Coerce percentage strings
        for col in df.columns:
            if df[col].dtype == object:
                sample = df[col].dropna().astype(str).head(3).tolist()
                if sample and any(s.endswith("%") for s in sample):
                    df[col] = df[col].apply(self._pct)
        return df

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
        df = df.rename(columns={"date": "Date", "open": "Open", "high": "High",
                                 "low": "Low", "close": "Close", "volume": "Volume"})
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


def diff_groups(today: dict, prior: dict, group_type: str = "sector",
                metric: str = "perf_w", top_n: int = 10) -> list[dict]:
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
        deltas.append({
            "name": name,
            "today": t.get(metric),
            "prior": p.get(metric),
            "delta": t.get(metric, 0) - p.get(metric, 0),
        })
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
        rows.append({
            "ticker": tk,
            "industry": ind,
            "ticker_w": float(tw),
            "industry_w": float(iw),
            "rs_spread": float(tw) - float(iw),
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("rs_spread", key=lambda s: s.abs(), ascending=False)
    return out
