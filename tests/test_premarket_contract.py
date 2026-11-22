"""The premarket gap contract (#51): no premarket price means ``gap_pct`` None, never 0.

A 0 is indistinguishable from a flat tape, and read as one it hid a closed market from
``comps`` (#48). Every provider returns ``has_premarket``, ``gap_pct`` and ``pre_price``, and
every consumer shows a missing gap as n/a instead of +0.0% or nan%.
"""

import datetime as dt
import io
import math
from types import SimpleNamespace

import pandas as pd
import pytest
from rich.console import Console

from tradekit.config import ET
from tradekit.data.finviz_elite import FinvizEliteProvider
from tradekit.data.massive import MassiveProvider
from tradekit.screener.filters import gap_filter

CONTRACT_KEYS = {"has_premarket", "gap_pct", "pre_price"}


def assert_no_premarket(data):
    assert CONTRACT_KEYS <= data.keys(), f"missing contract keys: {CONTRACT_KEYS - data.keys()}"
    assert data["has_premarket"] is False
    assert data["gap_pct"] is None
    assert data["pre_price"] is None


# ----- providers -----


def yahoo_with(monkeypatch, info):
    from tradekit.data import yahoo

    monkeypatch.setattr(yahoo.yf, "Ticker", lambda symbol: type("T", (), {"info": info})())
    return yahoo.YahooProvider()


def test_yahoo_without_premarket_returns_none_not_zero(monkeypatch):
    # A preMarketChangePercent left over from an earlier session must not leak through.
    provider = yahoo_with(monkeypatch, {"previousClose": 100.0, "preMarketChangePercent": 1.7})
    assert_no_premarket(provider.get_premarket("SOXX"))


def test_yahoo_with_premarket_still_reports_the_gap(monkeypatch):
    provider = yahoo_with(monkeypatch, {"previousClose": 100.0, "preMarketPrice": 102.0})
    data = provider.get_premarket("SOXX")
    assert (data["has_premarket"], data["pre_price"], data["gap_pct"]) == (True, 102.0, 2.0)


def test_yahoo_batch_skips_tickers_without_premarket(monkeypatch):
    provider = yahoo_with(monkeypatch, {"previousClose": 100.0})
    assert provider.get_multiple_premarket(["A", "B"]) == []


def test_finviz_elite_has_no_premarket_and_says_so(monkeypatch):
    provider = FinvizEliteProvider.__new__(FinvizEliteProvider)  # no token needed
    monkeypatch.setattr(provider, "get_quote", lambda t: {"price": 50.0, "ah_price": 50.5, "ah_change": 1.0})
    data = provider.get_premarket("XYZ")
    assert_no_premarket(data)
    assert data["ah_price"] == 50.5  # its after-hours fields are kept


#: Fri 2026-11-20 08:15 ET: the premarket is trading.
PREMARKET = dt.datetime(2026, 11, 20, 8, 15, tzinfo=ET)


def minute_bars(*rows):
    """(datetime, close, volume) rows as MassiveREST.aggs returns them."""
    index = (
        pd.DatetimeIndex([r[0] for r in rows], name="date").tz_convert("America/New_York")
        if rows
        else (pd.DatetimeIndex([], tz="America/New_York", name="date"))
    )
    closes = [r[1] for r in rows]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [r[2] for r in rows]}, index=index
    )


def massive_with(bars, prev_close=100.0, now=PREMARKET):
    """MassiveProvider over a fake REST client: no key, no network (#52)."""
    rest = SimpleNamespace(
        stock_snapshot=lambda t: {"prevDay": {"c": prev_close} if prev_close else {}},
        aggs=lambda *a, **k: bars,
    )
    return MassiveProvider(rest=rest, clock=lambda: now)


def test_massive_without_premarket_bars_returns_none_not_zero():
    last_night = minute_bars((PREMARKET - dt.timedelta(hours=12), 99.0, 500))  # yesterday's post-market
    assert_no_premarket(massive_with(last_night).get_premarket("SOXX"))


def test_massive_sums_the_premarket_minute_bars():
    bars = minute_bars(
        (PREMARKET.replace(hour=3, minute=59), 98.0, 9_999),  # before 04:00: not premarket
        (PREMARKET.replace(hour=4, minute=0), 100.5, 1_000),
        (PREMARKET.replace(hour=7, minute=30), 101.0, 2_500),
        (PREMARKET.replace(hour=8, minute=14), 101.5, 500),
    )
    data = massive_with(bars).get_premarket("SOXX")
    assert (data["has_premarket"], data["pre_price"], data["gap_pct"]) == (True, 101.5, 1.5)
    assert data["pre_volume"] == 4_000 and data["pre_bars"] == 3


def test_massive_after_the_open_is_not_premarket():
    bars = minute_bars((PREMARKET, 101.5, 1_000))
    data = massive_with(bars, now=PREMARKET.replace(hour=10)).get_premarket("SOXX")
    assert_no_premarket(data)
    assert data["prev_close"] == 100.0  # still reported


def test_massive_on_a_weekend_does_not_ask_for_bars():
    def never(*a, **k):
        raise AssertionError("no bars request outside the premarket window")

    rest = SimpleNamespace(stock_snapshot=lambda t: {"prevDay": {"c": 100.0}}, aggs=never)
    saturday = dt.datetime(2026, 11, 21, 8, 15, tzinfo=ET)
    assert_no_premarket(MassiveProvider(rest=rest, clock=lambda: saturday).get_premarket("SOXX"))


def test_massive_without_a_prior_close_has_no_gap():
    data = massive_with(minute_bars((PREMARKET, 101.5, 10)), prev_close=None).get_premarket("SOXX")
    assert data["has_premarket"] and data["gap_pct"] is None


def test_massive_batch_skips_tickers_without_premarket():
    assert massive_with(minute_bars()).get_multiple_premarket(["A", "B"]) == []


def test_backtest_may_refuse_premarket_outright():
    from tradekit.data.backtest import BacktestProvider

    provider = BacktestProvider.__new__(BacktestProvider)
    with pytest.raises(NotImplementedError):
        provider.get_premarket("SPY")


# ----- consumers -----


def test_gap_filter_never_passes_a_missing_gap():
    df = pd.DataFrame({"ticker": ["UP", "NONE", "NAN", "FLAT"], "gap_pct": [3.0, None, math.nan, 0.0]})
    assert gap_filter(2.0)(df)["ticker"].tolist() == ["UP"]
    assert gap_filter(0)(df)["ticker"].tolist() == ["UP", "FLAT"]  # a real 0.0 passes a 0 threshold


def scan_df():
    return pd.DataFrame(
        [
            {"ticker": "GAP", "name": "Gapper", "price": 10.0, "pre_price": 11.0, "gap_pct": 10.0},
            {"ticker": "NOPM", "name": "No premarket", "price": 20.0, "pre_price": None, "gap_pct": None},
        ]
    )


def test_terminal_scan_report_shows_n_a_for_a_missing_gap(monkeypatch):
    from tradekit.reports import terminal

    buf = io.StringIO()
    monkeypatch.setattr(terminal, "console", Console(file=buf, width=200, color_system=None))
    terminal.print_scan_results(scan_df())
    out = buf.getvalue()
    assert "+10.0%" in out
    assert "n/a" in out and "nan" not in out
    assert "20.00" in out  # NOPM falls back to the regular price


def test_markdown_scan_report_shows_n_a_for_a_missing_gap():
    from tradekit.reports.markdown import generate_scan_report

    out = generate_scan_report(scan_df())
    assert "| +10.0% |" in out
    assert "| **NOPM** | $20.00 | n/a |" in out  # falls back to the regular price, not $nan
    assert "nan" not in out and "+0.0%" not in out


def test_second_day_rows_say_premarket_is_n_a():
    from tradekit import cli as cli_mod

    class NoHistory:
        def get_history(self, *a, **k):
            raise RuntimeError("offline")

    df = pd.DataFrame(
        [
            {"ticker": "PM", "price": 10.0, "prev_change_pct": 12.0, "volume_ratio": 3.0, "pre_gap_pct": 1.5},
            {"ticker": "NOPM", "price": 10.0, "prev_change_pct": 12.0, "volume_ratio": 3.0, "pre_gap_pct": None},
        ]
    )
    rows = {r["ticker"]: r["notes"] for r in cli_mod._build_second_day_rows(df, NoHistory())}
    assert "pre-mkt +1.5%" in rows["PM"]
    assert "pre-mkt n/a" in rows["NOPM"] and "nan" not in rows["NOPM"]


def test_price_filter_falls_back_to_the_regular_price_without_a_premarket():
    from tradekit.screener.filters import price_filter

    df = pd.DataFrame(
        [
            {"ticker": "PM", "pre_price": 12.0, "price": 30.0},  # premarket price decides
            {"ticker": "NOPM", "pre_price": None, "price": 12.0},  # regular price decides
            {"ticker": "CHEAP", "pre_price": None, "price": 1.0},
            {"ticker": "NOPRICE", "pre_price": None, "price": None},
        ]
    )
    assert price_filter(5, 20)(df)["ticker"].tolist() == ["PM", "NOPM"]


def test_price_filter_without_pre_price_uses_price():
    from tradekit.screener.filters import price_filter

    df = pd.DataFrame({"ticker": ["A", "B"], "price": [4.0, 10.0]})
    assert price_filter(5)(df)["ticker"].tolist() == ["B"]


def test_second_day_rows_survive_a_session_with_no_premarket_at_all():
    """All gaps None (weekend, before 04:00 ET): the column holds None, not NaN, and `None > 0`
    used to raise TypeError in the bias line. Found in the #53 review."""
    from tradekit import cli as cli_mod

    class NoHistory:
        def get_history(self, *a, **k):
            raise RuntimeError("offline")

    df = pd.DataFrame(
        [
            {"ticker": "UP", "price": 10.0, "prev_change_pct": 12.0, "volume_ratio": 3.0, "pre_gap_pct": None},
            {"ticker": "DN", "price": 10.0, "prev_change_pct": -12.0, "volume_ratio": 3.0, "pre_gap_pct": None},
        ]
    )
    assert df["pre_gap_pct"].dtype == object  # the case that crashed
    rows = cli_mod._build_second_day_rows(df, NoHistory())
    assert [(r["ticker"], r["bias"]) for r in rows] == [("UP", "NEUT"), ("DN", "NEUT")]
    assert all("pre-mkt n/a" in r["notes"] for r in rows)


def test_second_day_bias_still_needs_the_premarket_to_confirm():
    from tradekit import cli as cli_mod

    class NoHistory:
        def get_history(self, *a, **k):
            raise RuntimeError("offline")

    df = pd.DataFrame(
        [
            {"ticker": "UP", "price": 10.0, "prev_change_pct": 12.0, "volume_ratio": 3.0, "pre_gap_pct": 2.0},
            {"ticker": "DN", "price": 10.0, "prev_change_pct": -12.0, "volume_ratio": 3.0, "pre_gap_pct": -2.0},
            {"ticker": "NA", "price": 10.0, "prev_change_pct": 12.0, "volume_ratio": 3.0, "pre_gap_pct": None},
        ]
    )
    bias = {r["ticker"]: r["bias"] for r in cli_mod._build_second_day_rows(df, NoHistory())}
    assert bias == {"UP": "BULL", "DN": "BEAR", "NA": "NEUT"}


@pytest.mark.parametrize("gaps", [[None, None], [3.0, None, -5.0]])
def test_scanner_sort_puts_missing_gaps_last_without_crashing(gaps):
    from tradekit.screener.premarket import _abs_gap

    df = pd.DataFrame({"gap_pct": gaps})
    order = df.sort_values("gap_pct", key=_abs_gap, ascending=False)["gap_pct"].tolist()
    present = [g for g in order if g is not None and not pd.isna(g)]
    assert present == sorted(present, key=abs, reverse=True)
    assert all(g is None or pd.isna(g) for g in order[len(present) :])
