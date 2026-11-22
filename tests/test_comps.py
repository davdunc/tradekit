"""Tests for the sector & overnight comps analysis (offline, fake provider)."""

import datetime as dt
from types import SimpleNamespace

import pytest

from tradekit.analysis.comps import (
    COUNTER_TREND_COUNT,
    ET,
    build_comps_report,
    comps_symbols,
    last_us_close,
)
from tradekit.data.batch import fetch_each


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


def fetch_report(provider, ticker, sector=None, *, now=None):
    """What the CLI does since #49: fetch the symbols comps needs, then build from them."""
    needed = comps_symbols(ticker, sector)
    if needed is None:
        return None
    quote_symbols, premarket_symbols = needed
    quotes = fetch_each(provider.get_quote, quote_symbols)
    premarket = fetch_each(provider.get_premarket, premarket_symbols)
    return build_comps_report(ticker, quotes, premarket, sector, now=now)


def test_mu_memory_scenario_forces_counter_trend_for_long():
    report = fetch_report(FakeProvider(), "MU")
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
    report = fetch_report(FakeProvider(), "MU")
    tell = next(s for s in report.signals if "SOXS" in s.name)
    assert tell.move_pct == -4.9  # +4.9% inverse ETF reads as -4.9% sector
    assert tell.bearish is True


def test_unknown_ticker_returns_none_without_sector_override():
    assert fetch_report(FakeProvider(), "ZZZZ") is None


def test_sector_override_maps_unknown_ticker():
    report = fetch_report(FakeProvider(), "ZZZZ", sector="memory")
    assert report is not None
    assert report.sector == "memory"


def test_flat_sector_only_trips_index_skew():
    report = fetch_report(FakeProvider(), "COIN")
    assert report is not None
    # COIN's own sector signals are flat/absent; the only bearish read is
    # the QQQ-led index skew (-0.59% with |QQQ-SPY| >= 0.25).
    assert report.bearish_count == 1
    assert report.against("short") == 0
    assert report.verdict("long").startswith("CAUTION")


def test_missing_data_is_no_read_not_aligned():
    """No data is not a neutral tape (#48): it used to read "ALIGNED: sector supports long"."""

    class EmptyProvider(FakeProvider):
        QUOTES = {}
        PREMARKET = {}

    report = fetch_report(EmptyProvider(), "MU")
    assert report is not None
    assert report.bearish_count == 0
    assert report.bullish_count == 0
    assert report.missing == len(report.signals) == 5
    assert report.verdict("long") == (
        "NO READ: none of the 5 signals is live, so COUNTER-TREND can't be ruled out — rerun once premarket is trading"
    )


# ----- #46 review: invalid direction and sector are errors, not wrong answers -----


def test_against_rejects_an_unknown_direction():
    from tradekit.analysis.comps import CompsReport

    report = CompsReport(ticker="MU", sector="memory", sector_label="Memory")
    assert report.against("long") == 0 and report.against("short") == 0
    with pytest.raises(ValueError, match="'long' or 'short'"):
        report.against("Long ")
    with pytest.raises(ValueError):
        report.verdict("flat")


def test_unknown_explicit_sector_raises_instead_of_reading_as_unmapped():
    from tradekit.analysis.comps import build_comps_report

    with pytest.raises(ValueError, match="unknown sector 'memroy'; choose from china, crypto, memory, semis"):
        build_comps_report("MU", {}, {}, sector="memroy")
    with pytest.raises(ValueError, match="unknown sector 'memroy'"):
        comps_symbols("MU", sector="memroy")


def test_unmapped_ticker_without_a_sector_still_returns_none():
    from tradekit.analysis.comps import build_comps_report

    assert build_comps_report("ZZZZ", {}, {}) is None
    assert comps_symbols("ZZZZ") is None


def test_cli_rejects_an_unknown_sector_before_fetching_anything(monkeypatch):
    from click.testing import CliRunner

    from tradekit import cli as cli_mod

    def no_provider(source):
        raise AssertionError("no data should be fetched for an invalid --sector")

    monkeypatch.setattr(cli_mod, "get_provider", no_provider)
    result = CliRunner().invoke(cli_mod.cli, ["comps", "MU", "--sector", "memroy"])
    assert result.exit_code == 2, result.output
    assert "'memroy' is not a sector; choose from china, crypto, memory, semis" in result.output


def test_cli_sector_is_case_insensitive(monkeypatch):
    from click.testing import CliRunner

    from tradekit import cli as cli_mod
    from tradekit.analysis import comps as comps_mod

    seen = {}
    empty = SimpleNamespace(get_quote=lambda s: {}, get_premarket=lambda s: {})
    monkeypatch.setattr(cli_mod, "get_provider", lambda source: empty)
    monkeypatch.setattr(comps_mod, "build_comps_report", lambda t, q, p, sector=None: seen.setdefault("sector", sector))
    result = CliRunner().invoke(cli_mod.cli, ["comps", "MU", "--sector", "MEMORY"])
    assert seen["sector"] == "MEMORY", result.output


# ----- #48: freshness — unavailable is not 0%, stale overnight comps don't count -----


SAT = dt.datetime(2026, 11, 21, 14, 0, tzinfo=ET)  # weekend
MON_0330 = dt.datetime(2026, 11, 23, 3, 30, tzinfo=ET)  # before premarket opens at 04:00
FRI_0100 = dt.datetime(2026, 11, 20, 1, 0, tzinfo=ET)  # Korea's Friday session, before Fri's US close
MON_0130 = dt.datetime(2026, 11, 23, 1, 30, tzinfo=ET)  # Korea's Monday session, after Fri's US close


@pytest.mark.parametrize(
    ("now", "close"),
    [
        (SAT, dt.datetime(2026, 11, 20, 16, 0, tzinfo=ET)),
        (MON_0330, dt.datetime(2026, 11, 20, 16, 0, tzinfo=ET)),
        (dt.datetime(2026, 11, 23, 15, 59, tzinfo=ET), dt.datetime(2026, 11, 20, 16, 0, tzinfo=ET)),
        (dt.datetime(2026, 11, 23, 16, 0, tzinfo=ET), dt.datetime(2026, 11, 23, 16, 0, tzinfo=ET)),
        (dt.datetime(2026, 11, 24, 21, 0, tzinfo=dt.UTC), dt.datetime(2026, 11, 24, 16, 0, tzinfo=ET)),
    ],
)
def test_last_us_close(now, close):
    assert last_us_close(now) == close


def closed_market(asia_time):
    """Yahoo outside premarket: has_premarket False with gap_pct 0; Asia quotes stamped ``asia_time``."""

    class Provider(FakeProvider):
        QUOTES = {
            "005930.KS": {"price": 93.0, "prev_close": 100.0, "market_time": asia_time.timestamp()},
            "000660.KS": {"price": 94.0, "prev_close": 100.0, "market_time": asia_time.timestamp()},
        }

        def get_premarket(self, ticker):
            return {"gap_pct": 0, "has_premarket": False}

    return Provider()


def test_no_premarket_session_is_unavailable_not_zero():
    report = fetch_report(closed_market(MON_0130), "MU", now=MON_0330)
    premarket = [s for s in report.signals if "overnight" not in s.name]
    assert [s.name for s in premarket] == ["SOXX premarket", "SOXS tell (sign flipped)", "Index skew (QQQ-led)"]
    assert all(s.move_pct is None and s.note == "no premarket session" for s in premarket)


def test_before_premarket_the_verdict_says_counter_trend_cannot_be_ruled_out():
    """Monday 03:30 ET: two live bearish Asia comps, three unavailable: the issue's example."""
    report = fetch_report(closed_market(MON_0130), "MU", now=MON_0330)
    assert report.bearish_count == 2 and report.missing == 3
    assert report.verdict("long") == (
        "CAUTION: 2 signal(s) against long (3 of 5 signals unavailable or stale)"
        " — too few live signals to rule out COUNTER-TREND"
    )


def test_weekend_overnight_comps_are_stale_and_not_counted():
    report = fetch_report(closed_market(FRI_0100), "MU", now=SAT)
    samsung = report.signals[0]
    assert samsung.stale and samsung.move_pct == pytest.approx(-7.0)
    assert samsung.note == "stale: Fri 11-20 01:00 ET, before the last US close (Fri 11-20)"
    assert samsung.bearish is None
    assert report.verdict("long").startswith("NO READ")  # the weekend run from #48 no longer says CAUTION


def test_fresh_overnight_comp_is_counted_and_stamped():
    report = fetch_report(closed_market(MON_0130), "MU", now=MON_0330)
    samsung = report.signals[0]
    assert samsung.live and samsung.bearish is True
    assert samsung.note == "Mon 11-23 01:30 ET"


def test_quote_without_a_market_time_counts_but_says_so():
    report = fetch_report(FakeProvider(), "MU", now=MON_0330)
    assert report.signals[0].note == "session time unknown"
    assert report.signals[0].live


def test_unavailable_signals_do_not_hide_a_counter_trend_call():
    """Enough live signals against still make COUNTER-TREND, with the gap stated."""

    class OneMissing(FakeProvider):
        PREMARKET = {**FakeProvider.PREMARKET, "SPY": {"gap_pct": None}}

    report = fetch_report(OneMissing(), "MU", now=MON_0330)
    verdict = report.verdict("long")
    assert verdict.startswith("COUNTER-TREND: 4 signals against long")
    assert verdict.endswith("(1 of 5 signals unavailable or stale)")


def test_aligned_verdict_also_flags_hidden_counter_trend():
    class Quiet(FakeProvider):
        QUOTES = {"005930.KS": {"price": 100.0, "prev_close": 100.0}}
        PREMARKET = {"SOXX": {"gap_pct": 0.1}}

    report = fetch_report(Quiet(), "MU", now=MON_0330)
    # 5 signals: Samsung and SOXX are live (both flat). Missing: SK Hynix (no quote), the SOXS
    # tell (no premarket), and the index skew, one signal built from QQQ *and* SPY. So 3.
    assert report.missing == 3
    assert report.verdict("long").endswith("— too few live signals to rule out COUNTER-TREND")


def test_yahoo_quote_carries_its_market_time(monkeypatch):
    from tradekit.data import yahoo

    class Ticker:
        def __init__(self, symbol):
            self.info = {"regularMarketPrice": 93.0, "previousClose": 100.0, "regularMarketTime": 1795586400}

    monkeypatch.setattr(yahoo.yf, "Ticker", Ticker)
    assert yahoo.YahooProvider().get_quote("005930.KS")["market_time"] == 1795586400


def test_cli_shows_stale_and_unavailable_rows(monkeypatch):
    import re

    from click.testing import CliRunner

    from tradekit import cli as cli_mod
    from tradekit.analysis import comps as comps_mod

    real = comps_mod.build_comps_report
    monkeypatch.setattr(cli_mod, "get_provider", lambda source: closed_market(FRI_0100))
    monkeypatch.setattr(comps_mod, "build_comps_report", lambda t, q, p, sector=None: real(t, q, p, sector, now=SAT))
    result = CliRunner().invoke(cli_mod.cli, ["comps", "MU"])
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    out = " ".join(re.sub(r"[│┃┏┓┗┛┡┩└┘├┤┼━─╇┳╋┻┬┴]", " ", plain).split())  # rejoin wrapped cells
    assert result.exit_code == 0, result.output
    assert "Samsung overnight -7.00% stale 005930.KS · stale: Fri 11-20 01:00 ET, before the last US close" in out
    assert "SOXX premarket - n/a sector ETF · no premarket session" in out
    assert "NO READ" in out and "+0.00%" not in out


# ----- #49: comps does no I/O; the CLI fetches each shared symbol once -----


def test_build_comps_report_works_on_plain_dicts_without_a_provider():
    quotes = {"005930.KS": {"price": 91.0, "prev_close": 100.0}, "000660.KS": {"price": 96.0, "prev_close": 100.0}}
    premarket = {
        "SOXX": {"has_premarket": True, "gap_pct": -1.6},
        "SOXS": {"has_premarket": True, "gap_pct": 4.9},
        "QQQ": {"has_premarket": True, "gap_pct": -0.59},
        "SPY": {"has_premarket": True, "gap_pct": -0.15},
    }
    report = build_comps_report("MU", quotes, premarket, now=MON_0330)
    assert report.verdict("long").startswith("COUNTER-TREND")


def test_comps_symbols_lists_what_to_fetch():
    assert comps_symbols("MU") == (["005930.KS", "000660.KS"], ["SOXX", "SOXS", "QQQ", "SPY"])


def test_a_failed_request_is_reported_not_raised():
    premarket = {"SOXX": {"error": "TimeoutError: read timed out"}}
    report = build_comps_report("MU", {}, premarket, now=MON_0330)
    soxx = next(s for s in report.signals if s.name == "SOXX premarket")
    assert soxx.move_pct is None and soxx.note == "premarket request failed"


def test_fetch_each_records_failures_and_skips_symbols_already_fetched():
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        if symbol == "BAD":
            raise TimeoutError("read timed out")
        return {"price": 1.0}

    out = fetch_each(fetch, ["QQQ", "BAD", "QQQ"])
    assert out["BAD"] == {"error": "TimeoutError: read timed out"}
    fetch_each(fetch, ["QQQ", "SPY"], into=out)
    assert calls == ["QQQ", "BAD", "SPY"]  # QQQ fetched once, even across calls


def test_cli_fetches_shared_symbols_once_across_tickers(monkeypatch):
    from click.testing import CliRunner

    from tradekit import cli as cli_mod

    calls = []

    class Counting(FakeProvider):
        def get_quote(self, ticker):
            calls.append(("quote", ticker))
            return super().get_quote(ticker)

        def get_premarket(self, ticker):
            calls.append(("premarket", ticker))
            return super().get_premarket(ticker)

    monkeypatch.setattr(cli_mod, "get_provider", lambda source: Counting())
    result = CliRunner().invoke(cli_mod.cli, ["comps", "MU", "WDC", "SNDK"])
    assert result.exit_code == 0, result.output
    assert len(calls) == len(set(calls)) == 6  # 2 Asia comps + SOXX, SOXS, QQQ, SPY, each once
