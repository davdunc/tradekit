"""Volume analysis: VWAP, relative volume, volume profile."""

import numpy as np
import pandas as pd
from ta.volume import VolumeWeightedAveragePrice

from tradekit.config import ET


def compute_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Compute VWAP (Volume Weighted Average Price).

    Note: this is a *rolling* VWAP (ta default window). For the session-anchored
    "New York VWAP" a day trader reads off the chart, use :func:`compute_session_vwap`.
    """
    vwap = VolumeWeightedAveragePrice(high=high, low=low, close=close, volume=volume)
    return vwap.volume_weighted_average_price()


def compute_session_vwap(
    df: pd.DataFrame,
    session_start: str = "09:30",
    session_end: str = "16:00",
) -> pd.Series:
    """Compute the New York session-anchored VWAP for intraday bars.

    Resets at the regular-session open (09:30 ET) each trading day and
    accumulates through the close (16:00 ET), matching the "New York VWAP"
    seen on a chart — unlike :func:`compute_vwap`, which is a rolling window.

    Args:
        df: Intraday OHLCV with a DatetimeIndex. A tz-naive index is assumed to
            be UTC (as the Massive provider returns) and converted to US/Eastern;
            a tz-aware index is converted from its own zone.
        session_start: Regular-session open in ET, "HH:MM".
        session_end: Regular-session close in ET, "HH:MM".

    Returns:
        A Series aligned to ``df.index``. Bars outside the regular session are
        NaN — they are not part of the session VWAP.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("compute_session_vwap requires a DatetimeIndex")

    idx = df.index
    et_index = idx.tz_localize("UTC").tz_convert(ET) if idx.tz is None else idx.tz_convert(ET)

    start = pd.to_datetime(session_start).time()
    end = pd.to_datetime(session_end).time()
    in_session = pd.Series([start <= t <= end for t in et_index.time], index=df.index)

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = typical * df["volume"]
    session_date = pd.Series(et_index.normalize(), index=df.index)

    work = pd.DataFrame({"tpv": tpv, "vol": df["volume"], "date": session_date})[in_session]
    cum_tpv = work.groupby("date")["tpv"].cumsum()
    cum_vol = work.groupby("date")["vol"].cumsum()

    vwap = pd.Series(np.nan, index=df.index, name="session_vwap")
    vwap.loc[work.index] = cum_tpv / cum_vol.replace(0, np.nan)
    return vwap


def compute_relative_volume(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Compute relative volume vs N-day average."""
    avg_vol = volume.rolling(window=lookback).mean()
    return volume / avg_vol


def compute_volume_profile(close: pd.Series, volume: pd.Series, bins: int = 20) -> pd.DataFrame:
    """Compute a volume profile (price-at-volume histogram).

    Returns DataFrame with columns: price_level, volume, pct_of_total.
    """
    price_min = close.min()
    price_max = close.max()
    bin_edges = np.linspace(price_min, price_max, bins + 1)

    levels = []
    total_vol = volume.sum()

    for i in range(bins):
        low_edge = bin_edges[i]
        high_edge = bin_edges[i + 1]
        mask = (close >= low_edge) & (close < high_edge)
        vol_at_level = volume[mask].sum()
        mid_price = (low_edge + high_edge) / 2

        levels.append(
            {
                "price_level": round(mid_price, 2),
                "volume": int(vol_at_level),
                "pct_of_total": round(vol_at_level / total_vol * 100, 2) if total_vol > 0 else 0,
            }
        )

    return pd.DataFrame(levels).sort_values("volume", ascending=False).reset_index(drop=True)


def find_high_volume_nodes(close: pd.Series, volume: pd.Series, bins: int = 20, top_n: int = 3) -> list[float]:
    """Find the top N high-volume price levels (HVN) from volume profile."""
    profile = compute_volume_profile(close, volume, bins)
    return profile.head(top_n)["price_level"].tolist()


def add_volume_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add volume indicators to OHLCV DataFrame."""
    result = df.copy()
    result["vwap"] = compute_vwap(df["high"], df["low"], df["close"], df["volume"])
    result["relative_volume"] = compute_relative_volume(df["volume"])
    result["volume_sma_20"] = df["volume"].rolling(20).mean()
    return result
