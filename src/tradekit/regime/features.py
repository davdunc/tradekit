"""Pure feature functions over daily bars. No I/O, no clock: same input, same output.

Bars are a DataFrame with a ``date`` column (datetime.date) and ``open, high, low, close, volume``,
sorted ascending. Every function returns ``None`` when there is not enough data. Callers turn that
into ``unknown``.
"""

from __future__ import annotations

import math
import re

import pandas as pd


def atr(bars: pd.DataFrame, window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    prev_close = bars["close"].shift(1)
    tr = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev_close).abs(), (bars["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    val = tr.iloc[-window:].mean()
    return float(val) if val > 0 else None


def close_vs_sma(bars: pd.DataFrame, sma_window: int, atr_window: int) -> float | None:
    """(close − SMA_n) / ATR, in ATR units."""
    a = atr(bars, atr_window)
    if a is None or len(bars) < sma_window:
        return None
    sma = bars["close"].iloc[-sma_window:].mean()
    return float((bars["close"].iloc[-1] - sma) / a)


def sma_slope(bars: pd.DataFrame, sma_window: int, lag: int, atr_window: int) -> float | None:
    """(SMA_n[t] − SMA_n[t−lag]) / ATR, in ATR units."""
    a = atr(bars, atr_window)
    if a is None or len(bars) < sma_window + lag:
        return None
    sma = bars["close"].rolling(sma_window).mean()
    return float((sma.iloc[-1] - sma.iloc[-1 - lag]) / a)


def efficiency_ratio(bars: pd.DataFrame, window: int) -> float | None:
    """|C_t − C_{t−n}| / Σ|ΔC| over n sessions; None when there is no movement at all (denominator 0)."""
    if len(bars) < window + 1:
        return None
    c = bars["close"].iloc[-(window + 1) :]
    path = c.diff().abs().sum()
    if path == 0:
        return None
    return float(abs(c.iloc[-1] - c.iloc[0]) / path)


def hv_series(bars: pd.DataFrame, window: int) -> pd.Series:
    """Annualized close-to-close realized volatility (%), rolling."""
    r = (bars["close"] / bars["close"].shift(1)).apply(lambda x: math.log(x) if x and x > 0 else float("nan"))
    return r.rolling(window).std() * math.sqrt(252) * 100


def hv_percentile(bars: pd.DataFrame, window: int, lookback: int) -> tuple[float, float] | None:
    """(today's HV, its percentile 0–100 within the trailing ``lookback`` HV values)."""
    hv = hv_series(bars, window).dropna()
    if len(hv) < lookback:
        return None
    hist = hv.iloc[-lookback:]
    today = hist.iloc[-1]
    pct = float((hist <= today).mean() * 100)
    return float(today), pct


def session_change(bars: pd.DataFrame) -> float | None:
    if len(bars) < 2:
        return None
    return float(bars["close"].iloc[-1] / bars["close"].iloc[-2] - 1)


def filter_universe(
    grouped: pd.DataFrame, min_price: float, min_volume: float, exclude_suffix_regex: str
) -> pd.DataFrame:
    """Common-stock-like names with enough price and volume (grouped columns: T, o, c, v)."""
    rx = re.compile(exclude_suffix_regex)
    g = grouped.dropna(subset=["T", "o", "c", "v"])
    g = g[(g["c"] >= min_price) & (g["v"] >= min_volume)]
    return g[~g["T"].astype(str).str.contains(rx)]


def name_agreement(universe: pd.DataFrame, sign: int) -> float | None:
    """Share of names whose session (close vs open) moved with ``sign``; v1 proxy for advancers."""
    if universe.empty or sign == 0:
        return None
    moved = (universe["c"] - universe["o"]).apply(lambda d: (d > 0) - (d < 0))
    return float((moved == sign).mean())


def sector_agreement(grouped: pd.DataFrame, sectors: list[str], sign: int) -> tuple[float, int] | None:
    """(share of sector ETFs moving with ``sign`` close vs open, count found)."""
    s = grouped[grouped["T"].isin(sectors)]
    if s.empty or sign == 0:
        return None
    moved = (s["c"] - s["o"]).apply(lambda d: (d > 0) - (d < 0))
    return float((moved == sign).mean()), len(s)
