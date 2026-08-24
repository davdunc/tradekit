"""Tests for the sector & overnight comps analysis (offline, fake provider)."""

from tradekit.analysis.comps import (
    COUNTER_TREND_COUNT,
    build_comps_report,
)


class FakeProvider:
    """Canned quotes reproducing the 2026-08-24 MU premarket picture."""

    QUOTES = {
        "005930.KS": {"price": 91.0, "prev_close": 100.0},  # Samsung -9% overnight
        "000660.KS": {"price": 96.0, "prev_close": 100.0},  # SK Hynix -4%
        "TSM": {"price": 100.0, "prev_close": 100.0},
    }
    PREMARKET = {
        "SOXX": {"gap_pct": -1.6},
        "SOXS": {"gap_pct": 4.9},  # inverse ETF up = bearish tell
        "QQQ": {"gap_pct": -0.59},
        "SPY": {"gap_pct": -0.15},
        "FXI": {"gap_pct": 0.0},
        "YANG": {"gap_pct": 0.0},
        "IBIT": {"gap_pct": 0.0},
        "BITI": {"gap_pct": 0.0},
        "BTC-USD": {"gap_pct": 0.0},
    }

    def get_quote(self, ticker):
        return self.QUOTES.get(ticker, {"price": 0, "prev_close": 0})

    def get_premarket(self, ticker):
        return self.PREMARKET.get(ticker, {"gap_pct": None})

    def get_history(self, ticker, period="3mo", interval="1d"):  # pragma: no cover
        raise NotImplementedError


def test_mu_memory_scenario_forces_counter_trend_for_long():
    report = build_comps_report(FakeProvider(), "MU")
    assert report is not None
    assert report.sector == "memory"
    names = [s.name for s in report.signals]
    assert any("Samsung" in n for n in names)
    assert any("SOXS" in n for n in names)
    # Samsung -9%, SK Hynix -4%, SOXX -1.6%, SOXS tell (flipped -4.9%),
    # QQQ-led skew -0.59% -> 5 bearish signals.
    assert report.bearish_count >= COUNTER_TREND_COUNT
    assert report.verdict("long").startswith("COUNTER-TREND")
    # Same tape supports a short thesis.
    assert report.against("short") == 0
    assert report.verdict("short").startswith("ALIGNED")


def test_inverse_etf_sign_is_flipped():
    report = build_comps_report(FakeProvider(), "MU")
    tell = next(s for s in report.signals if "SOXS" in s.name)
    assert tell.move_pct == -4.9  # +4.9% inverse ETF reads as -4.9% sector
    assert tell.bearish is True


def test_unknown_ticker_returns_none_without_sector_override():
    assert build_comps_report(FakeProvider(), "ZZZZ") is None


def test_sector_override_maps_unknown_ticker():
    report = build_comps_report(FakeProvider(), "ZZZZ", sector="memory")
    assert report is not None
    assert report.sector == "memory"


def test_flat_sector_only_trips_index_skew():
    report = build_comps_report(FakeProvider(), "COIN")
    assert report is not None
    # COIN's own sector signals are flat/absent; the only bearish read is
    # the QQQ-led index skew (-0.59% with |QQQ-SPY| >= 0.25).
    assert report.bearish_count == 1
    assert report.against("short") == 0
    assert report.verdict("long").startswith("CAUTION")


def test_missing_data_yields_neutral_signals():
    class EmptyProvider(FakeProvider):
        QUOTES = {}
        PREMARKET = {}

    report = build_comps_report(EmptyProvider(), "MU")
    assert report is not None
    assert report.bearish_count == 0
    assert report.bullish_count == 0
    assert report.verdict("long").startswith("ALIGNED")
