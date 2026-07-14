"""Trade-setup detectors that combine indicators into a single signal."""

import pandas as pd

from tradekit.analysis.indicators import compute_moving_averages
from tradekit.analysis.volume import compute_session_vwap


def detect_vwap_sandwich(
    df: pd.DataFrame,
    fast_ema: int = 9,
    slow_sma: int = 34,
    session_start: str = "09:30",
    session_end: str = "16:00",
) -> dict:
    """Detect the 15-Minute VWAP Sandwich on the most recent session bar.

    State setup: the fast EMA and the slow SMA close on OPPOSITE sides of the
    New York session VWAP. Direction-agnostic — bias is inferred from which
    average sits above VWAP: bullish when the fast EMA leads above, bearish
    when it leads below. This is the *state* that typically resolves into the
    "Fashionably Late" 9-EMA x VWAP cross *event* (see PlaybookSetups.md).

    Intended for 15-minute regular-session bars (e.g. Massive ``interval="15m"``).

    Args:
        df: Intraday OHLCV with a DatetimeIndex (see :func:`compute_session_vwap`).
        fast_ema: Fast EMA period (default 9).
        slow_sma: Slow SMA period (default 34).
        session_start: Regular-session open in ET, "HH:MM".
        session_end: Regular-session close in ET, "HH:MM".

    Returns:
        dict with keys:
            sandwich (bool)      — averages straddle session VWAP on the eval bar
            direction (str|None) — "bullish", "bearish", or None
            ema, sma, vwap, close (float|None) — values on the eval bar
            spread_pct (float|None) — (ema - sma) / vwap * 100
            detail (str)         — human-readable status
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"detect_vwap_sandwich missing columns: {sorted(missing)}")

    empty = {
        "sandwich": False,
        "direction": None,
        "ema": None,
        "sma": None,
        "vwap": None,
        "close": None,
        "spread_pct": None,
        "detail": "insufficient data",
    }
    if df.empty:
        return empty

    mas = compute_moving_averages(
        df["close"],
        configs=[{"period": fast_ema, "type": "ema"}, {"period": slow_sma, "type": "sma"}],
    )
    ema = mas[f"ema_{fast_ema}"]
    sma = mas[f"sma_{slow_sma}"]
    vwap = compute_session_vwap(df, session_start=session_start, session_end=session_end)

    valid = ema.notna() & sma.notna() & vwap.notna()
    if not valid.any():
        return empty

    i = valid[valid].index[-1]
    e, s, v, c = float(ema.loc[i]), float(sma.loc[i]), float(vwap.loc[i]), float(df["close"].loc[i])

    if e > v and s < v:
        sandwich, direction = True, "bullish"
    elif e < v and s > v:
        sandwich, direction = True, "bearish"
    else:
        sandwich, direction = False, None

    spread_pct = (e - s) / v * 100 if v else None

    if sandwich:
        rel_ema = ">" if e > v else "<"
        rel_sma = ">" if v > s else "<"
        detail = f"{direction} sandwich: {fast_ema}EMA {rel_ema} VWAP {rel_sma} {slow_sma}SMA"
    else:
        detail = "no sandwich — both averages on the same side of session VWAP"

    return {
        "sandwich": sandwich,
        "direction": direction,
        "ema": round(e, 4),
        "sma": round(s, 4),
        "vwap": round(v, 4),
        "close": round(c, 4),
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "detail": detail,
    }
