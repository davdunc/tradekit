"""Tests for the analysis engine."""

import numpy as np
import pandas as pd
import pytest

from tradekit.analysis.indicators import (
    compute_all_indicators,
    compute_macd,
    compute_moving_averages,
    compute_rsi,
    compute_stochastic,
)
from tradekit.analysis.levels import (
    cluster_levels,
    compute_pivot_points,
    find_local_extremes,
    find_support_resistance,
)
from tradekit.analysis.scoring import compute_composite_score, score_momentum, score_trend
from tradekit.analysis.setups import detect_vwap_sandwich
from tradekit.analysis.volume import compute_relative_volume, compute_session_vwap, compute_volume_profile


def _make_ohlcv(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """Create synthetic OHLCV data for testing."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.5, 2, n)
    low = close - rng.uniform(0.5, 2, n)
    open_ = close + rng.normal(0, 0.5, n)
    volume = rng.integers(100_000, 1_000_000, n).astype(float)

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


class TestIndicators:
    def test_rsi_range(self):
        df = _make_ohlcv()
        rsi = compute_rsi(df["close"])
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_macd_columns(self):
        df = _make_ohlcv()
        macd_df = compute_macd(df["close"])
        assert set(macd_df.columns) == {"macd", "macd_signal", "macd_histogram"}

    def test_stochastic_range(self):
        df = _make_ohlcv()
        stoch = compute_stochastic(df["high"], df["low"], df["close"])
        assert "stoch_k" in stoch.columns
        assert "stoch_d" in stoch.columns

    def test_moving_averages_default(self):
        df = _make_ohlcv()
        ma_df = compute_moving_averages(df["close"])
        assert "ema_9" in ma_df.columns
        assert "sma_200" in ma_df.columns

    def test_compute_all_indicators(self):
        df = _make_ohlcv()
        result = compute_all_indicators(df)
        assert "rsi" in result.columns
        assert "macd" in result.columns
        assert "ema_9" in result.columns
        assert "roc_10" in result.columns


class TestLevels:
    def test_pivot_points(self):
        pivots = compute_pivot_points(high=110, low=90, close=100)
        assert pivots["pivot"] == 100.0
        assert pivots["r1"] > pivots["pivot"]
        assert pivots["s1"] < pivots["pivot"]

    def test_find_local_extremes(self):
        # Create a series with obvious peaks and troughs
        values = [10, 20, 30, 20, 10, 20, 30, 40, 30, 20]
        s = pd.Series(values)
        peaks, troughs = find_local_extremes(s, order=2)
        assert len(peaks) > 0 or len(troughs) > 0

    def test_cluster_levels(self):
        levels = [100.0, 100.5, 101.0, 110.0, 110.5]
        clusters = cluster_levels(levels, tolerance_pct=1.5)
        assert len(clusters) == 2  # two clusters

    def test_find_support_resistance(self):
        df = _make_ohlcv(200)
        sr = find_support_resistance(df)
        assert "resistance" in sr
        assert "support" in sr


class TestScoring:
    def test_score_momentum_neutral(self):
        row = pd.Series({"rsi": 50, "macd_histogram": 0, "stoch_k": 50, "stoch_d": 50, "roc_10": 0})
        score = score_momentum(row)
        assert 0 <= score <= 100

    def test_score_trend_bullish(self):
        row = pd.Series(
            {
                "close": 100,
                "ema_9": 99,
                "ema_20": 98,
                "sma_50": 95,
                "sma_200": 90,
            }
        )
        score = score_trend(row)
        assert score > 50  # should be bullish

    def test_composite_score_structure(self):
        row = pd.Series(
            {
                "close": 100,
                "rsi": 55,
                "macd_histogram": 0.5,
                "stoch_k": 60,
                "stoch_d": 55,
                "roc_10": 3.0,
                "ema_9": 99,
                "ema_20": 98,
                "sma_50": 95,
                "sma_200": 90,
                "relative_volume": 2.0,
                "vwap": 99,
            }
        )
        result = compute_composite_score(row)
        assert "total" in result
        assert "grade" in result
        assert result["grade"] in ("A", "B", "C", "F")


class TestVolume:
    def test_relative_volume(self):
        volume = pd.Series([100_000] * 20 + [300_000])
        rvol = compute_relative_volume(volume)
        assert rvol.iloc[-1] > 2.5  # 3x average

    def test_volume_profile(self):
        df = _make_ohlcv()
        profile = compute_volume_profile(df["close"], df["volume"], bins=10)
        assert len(profile) == 10
        assert "price_level" in profile.columns
        assert "volume" in profile.columns


def _make_intraday(closes: np.ndarray, start: str = "2026-06-01 13:30", freq: str = "15min") -> pd.DataFrame:
    """Synthetic intraday OHLCV. Index is tz-naive UTC (as Massive returns).

    Default start 13:30 UTC == 09:30 ET (EDT, the RTH open) on a June weekday.
    """
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.25,
            "low": closes - 0.25,
            "close": closes,
            "volume": np.full(n, 100_000.0),
        },
        index=idx,
    )


class TestSessionVWAP:
    def test_first_session_bar_equals_typical_price(self):
        df = _make_intraday(np.array([100.0, 101.0, 102.0]))
        vwap = compute_session_vwap(df)
        # First in-session bar: cumulative VWAP == that bar's typical price (h+l+c)/3
        first = df.iloc[0]
        assert vwap.iloc[0] == pytest.approx((first["high"] + first["low"] + first["close"]) / 3.0)

    def test_premarket_bars_are_nan(self):
        # 13:00 / 13:15 UTC == 09:00 / 09:15 ET (pre-market); 13:30 == 09:30 (RTH open).
        df = _make_intraday(np.array([99.0, 100.0, 101.0]), start="2026-06-01 13:00")
        vwap = compute_session_vwap(df)
        assert np.isnan(vwap.iloc[0])  # 09:00 ET — pre-market, excluded
        assert np.isnan(vwap.iloc[1])  # 09:15 ET — pre-market, excluded
        assert vwap.iloc[2] == pytest.approx(101.0)  # 09:30 ET — first session bar

    def test_vwap_resets_each_session(self):
        day1 = _make_intraday(np.array([100.0, 100.0]), start="2026-06-01 13:30")
        day2 = _make_intraday(np.array([200.0, 200.0]), start="2026-06-02 13:30")
        df = pd.concat([day1, day2])
        vwap = compute_session_vwap(df)
        # Day 2's first bar must anchor to day 2, not carry day 1's ~100 level.
        assert vwap.iloc[2] == pytest.approx(200.0)


class TestSetups:
    def test_detect_vwap_sandwich_keys(self):
        df = _make_intraday(100 + np.cumsum(np.random.default_rng(1).normal(0, 0.5, 60)))
        result = detect_vwap_sandwich(df, session_start="00:00", session_end="23:59")
        assert set(result) == {"sandwich", "direction", "ema", "sma", "vwap", "close", "spread_pct", "detail"}
        assert isinstance(result["sandwich"], bool)
        assert result["direction"] in ("bullish", "bearish", None)

    def test_detect_bullish_sandwich_on_reclaim(self):
        # V-shape reclaim: high open -> sell off -> recover. Session VWAP (anchored at
        # the high open) ends up BETWEEN a rising 9-EMA (above) and a lagging 34-SMA
        # (below) -> bullish sandwich. A steady trend does NOT produce this.
        closes = np.concatenate([np.linspace(120.0, 100.0, 20), np.linspace(100.0, 119.0, 20)])
        df = _make_intraday(closes, start="2026-06-01 04:00")  # 00:00 ET, single session
        result = detect_vwap_sandwich(df, session_start="00:00", session_end="23:59")
        assert result["sandwich"] is True
        assert result["direction"] == "bullish"
        assert result["ema"] > result["vwap"] > result["sma"]

    def test_detect_bearish_sandwich_on_breakdown(self):
        # ^-shape breakdown: low open -> rally -> fade. 34-SMA (above) > VWAP > a
        # falling 9-EMA (below) -> bearish sandwich.
        closes = np.concatenate([np.linspace(100.0, 120.0, 20), np.linspace(120.0, 101.0, 20)])
        df = _make_intraday(closes, start="2026-06-01 04:00")
        result = detect_vwap_sandwich(df, session_start="00:00", session_end="23:59")
        assert result["sandwich"] is True
        assert result["direction"] == "bearish"
        assert result["sma"] > result["vwap"] > result["ema"]

    def test_insufficient_data_returns_no_sandwich(self):
        df = _make_intraday(np.array([100.0, 101.0, 102.0]))  # < 34 bars for the SMA
        result = detect_vwap_sandwich(df)
        assert result["sandwich"] is False
        assert result["direction"] is None
