"""Overnight-risk check: the carry math, verdicts, warnings, and the CLI without a broker."""

import datetime as dt
import json
import math
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from tradekit import cli as cli_mod
from tradekit.analysis.overnight import (
    BorrowInfo,
    DividendInfo,
    OvernightError,
    borrow_status,
    check_carry,
    expected_move,
    hold_end,
)

# ----- expected move -----


def test_expected_move_matches_the_worked_aapl_example():
    # AAPL 2026-11-20: spot 344.87, IV 0.3012 → 344.87 × 0.3012 ÷ √252 ≈ 6.54 (1.9%)
    assert expected_move(344.87, 0.3012) == pytest.approx(6.5434, abs=1e-3)


def test_expected_move_scales_with_the_square_root_of_sessions():
    assert expected_move(100, 0.4, 4) == pytest.approx(2 * expected_move(100, 0.4, 1))


@pytest.mark.parametrize(("price", "iv", "sessions"), [(0, 0.3, 1), (100, 0, 1), (100, 0.3, 0), (-1, 0.3, 1)])
def test_expected_move_rejects_non_positive_inputs(price, iv, sessions):
    with pytest.raises(OvernightError):
        expected_move(price, iv, sessions)


# ----- sizing and verdicts -----

# price 100, IV 0.3175: 1σ = 100 × 0.3175 ÷ √252 = 2.0001 → round numbers below
IV_2 = 2.0 * math.sqrt(252) / 100  # IV that makes 1σ exactly 2.00 on a 100 stock


def test_gap_not_stop_sets_the_loss_when_the_stop_is_tight():
    r = check_carry("xyz", 100, 99, 100, IV_2)  # stop 1 away, 2σ gap = 4
    assert r.symbol == "XYZ" and r.direction == "long"
    assert (r.stop_distance, r.one_sigma, r.gap_move, r.loss_per_share) == pytest.approx((1, 2, 4, 4))
    assert r.carry_shares == 25  # 100 ÷ 4, not 100 ÷ 1
    assert r.verdict == "SIZE"


def test_stop_sets_the_loss_when_it_is_wider_than_the_gap():
    r = check_carry("XYZ", 100, 94, 120, IV_2)  # stop 6 away > 4
    assert r.loss_per_share == pytest.approx(6)
    assert r.carry_shares == 20
    assert r.warnings == []


def test_short_direction_from_the_stop_side():
    r = check_carry("XYZ", 100, 103, 100, IV_2)
    assert r.direction == "short"
    assert r.loss_per_share == pytest.approx(4)


@pytest.mark.parametrize(
    ("shares", "verdict", "gap_loss"),
    [(25, "CARRY", 100.0), (10, "CARRY", 40.0), (40, "REDUCE", 160.0), (0, "CARRY", 0.0)],
)
def test_verdict_for_a_given_position(shares, verdict, gap_loss):
    r = check_carry("XYZ", 100, 99, 100, IV_2, shares=shares)
    assert r.verdict == verdict
    assert r.gap_loss == pytest.approx(gap_loss)


def test_no_carry_when_not_one_share_fits():
    r = check_carry("XYZ", 100, 99, 3, IV_2, shares=10)  # 3 ÷ 4 < 1
    assert (r.carry_shares, r.verdict) == (0, "NO CARRY")


def test_weekend_style_multi_session_hold_needs_a_smaller_carry():
    one = check_carry("XYZ", 100, 99, 100, IV_2, sessions=1)
    three = check_carry("XYZ", 100, 99, 100, IV_2, sessions=3)
    assert three.one_sigma == pytest.approx(one.one_sigma * math.sqrt(3), rel=1e-3)
    assert three.carry_shares < one.carry_shares


def test_zero_gap_sigmas_falls_back_to_the_stop_distance():
    r = check_carry("XYZ", 100, 99, 100, IV_2, gap_sigmas=0)
    assert r.loss_per_share == pytest.approx(1)
    assert r.carry_shares == 100


# ----- warnings never change the verdict -----


def test_stop_inside_one_sigma_warns():
    r = check_carry("XYZ", 100, 99, 100, IV_2, shares=10)
    assert any("inside a 1σ move" in w for w in r.warnings)
    assert r.verdict == "CARRY"


def test_event_priced_warns_at_the_ratio():
    r = check_carry("XYZ", 100, 94, 120, 0.50, hv=0.40)  # IV/HV 1.25
    assert r.iv_hv_ratio == pytest.approx(1.25)
    assert any("pricing an event" in w for w in r.warnings)


def test_ordinary_iv_hv_does_not_warn():
    r = check_carry("AAPL", 344.87, 330, 300, 0.2397, hv=0.2905)  # 11-19: IV/HV 0.83
    assert r.iv_hv_ratio == pytest.approx(0.8251, abs=1e-3)
    assert not any("event" in w for w in r.warnings)


def test_without_hv_there_is_no_ratio():
    assert check_carry("XYZ", 100, 94, 120, IV_2).iv_hv_ratio is None


# ----- invalid inputs -----


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"risk": 0}, "risk"),
        ({"stop": 100}, "invalidation"),
        ({"shares": -1}, "negative"),
        ({"gap_sigmas": -1}, "negative"),
        ({"hv": 0}, "HV"),
        ({"iv": 0}, "positive"),
    ],
)
def test_invalid_inputs(kwargs, match):
    args = {"symbol": "XYZ", "price": 100, "stop": 99, "risk": 100, "iv": IV_2, **kwargs}
    with pytest.raises(OvernightError, match=match):
        check_carry(**args)


# ----- CLI -----


def run(args):
    return CliRunner().invoke(cli_mod.cli, ["overnight", *args])


def test_cli_manual_inputs_need_no_broker_and_emit_json():
    result = run(
        ["aapl", "--stop", "335", "--risk", "100", "--shares", "20", "--price", "344.87", "--iv", "0.3012", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["symbol"] == "AAPL" and data["source"] == "given"
    assert data["one_sigma"] == pytest.approx(6.5434, abs=1e-3)
    assert data["loss_per_share"] == pytest.approx(13.0868, abs=1e-3)  # 2σ gap beats the 9.87 stop
    assert data["carry_shares"] == 7
    assert data["verdict"] == "REDUCE"


def test_cli_text_states_the_verdict_and_warnings():
    result = run(["XYZ", "--stop", "99", "--risk", "100", "--price", "100", "--iv", str(IV_2), "--hv", "0.2"])
    assert result.exit_code == 0, result.output
    assert "carry size 25 shares" in result.output
    assert "inside a 1σ move" in result.output
    assert "SIZE — carry at most 25 shares" in result.output


def test_cli_bad_inputs_are_a_usage_error():
    result = run(["XYZ", "--stop", "100", "--risk", "100", "--price", "100", "--iv", "0.3"])
    assert result.exit_code == 2
    assert "invalidation" in result.output


def test_cli_without_iv_and_without_ibkr_settings_says_what_to_set(monkeypatch):
    from tradekit import config

    data = SimpleNamespace(ibkr_host="", ibkr_port=None, ibkr_client_id=17)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data=data))
    result = run(["XYZ", "--stop", "99", "--risk", "100", "--price", "100"])
    assert result.exit_code == 1
    assert "IBKR_HOST and IBKR_PORT" in result.output
    assert "--price and --iv" in result.output


def test_cli_uses_the_ibkr_snapshot_and_reports_its_freshness(monkeypatch):
    from tradekit.data import ibkr

    snap = ibkr.VolSnapshot("XYZ", 100.0, IV_2, 0.2, "frozen")
    monkeypatch.setattr(ibkr, "fetch_vol_snapshot", lambda symbol: snap)
    result = run(["xyz", "--stop", "99", "--risk", "100", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["source"] == "IB Gateway (frozen)"
    assert (data["price"], data["hv"], data["carry_shares"]) == (100.0, 0.2, 25)


def test_cli_given_price_overrides_the_snapshot(monkeypatch):
    from tradekit.data import ibkr

    monkeypatch.setattr(ibkr, "fetch_vol_snapshot", lambda symbol: ibkr.VolSnapshot("XYZ", 50.0, IV_2, None, "live"))
    result = run(["XYZ", "--stop", "99", "--risk", "100", "--price", "100", "--json"])
    assert json.loads(result.stdout)["price"] == 100.0


# ----- short side: hold window, ex-dividend, borrow -----

FRI, SAT, MON, TUE, THU = (dt.date(2026, 11, d) for d in (20, 21, 23, 24, 26))


@pytest.mark.parametrize(
    ("as_of", "sessions", "end"),
    [(FRI, 1, MON), (SAT, 1, MON), (MON, 1, TUE), (THU, 3, dt.date(2026, 12, 1)), (MON, 1.5, dt.date(2026, 11, 25))],
)
def test_hold_end_skips_weekends(as_of, sessions, end):
    assert hold_end(as_of, sessions) == end


@pytest.mark.parametrize(
    ("level", "status"),
    [
        (3.0, "available"),
        (2.6, "available"),
        (2.5, "hard to borrow"),
        (2.0, "hard to borrow"),
        (1.5, "not available"),
        (0.0, "not available"),
    ],
)
def test_borrow_status_follows_ibkrs_shortable_levels(level, status):
    assert borrow_status(level) == status


def short(**kw):
    """A short of 10 at 100 with a 103 stop, carried from Friday 11-20 over the weekend."""
    args = {"shares": 10, "as_of": FRI, "dividend": DividendInfo(None, None), "borrow": BorrowInfo(3.0, 1_000_000)}
    return check_carry("XYZ", 100, 103, 100, IV_2, **{**args, **kw})


def test_short_with_clean_borrow_and_no_dividend_has_no_short_warnings():
    r = short()
    assert (r.hold_end, r.dividend_checked, r.dividend_in_hold, r.borrow_status) == (MON, True, False, "available")
    assert r.warnings == []


def test_ex_dividend_inside_the_hold_warns_and_totals_what_the_short_pays():
    r = short(dividend=DividendInfo(MON, 1.8311))  # SPY's real next dividend amount
    assert r.dividend_in_hold is True
    assert r.dividend_owed == pytest.approx(18.31)
    assert any("ex-dividend 2026-11-23 is inside the hold" in w and "18.31 on 10 shares" in w for w in r.warnings)
    assert r.verdict == "CARRY"  # a warning, not a size change


def test_ex_dividend_today_is_not_in_the_carry():
    """A short opened today wasn't held at the close before today's ex-date."""
    r = short(dividend=DividendInfo(FRI, 1.0))
    assert r.dividend_in_hold is False and r.dividend_owed is None


def test_ex_dividend_after_the_hold_is_reported_but_not_warned():
    r = short(dividend=DividendInfo(dt.date(2026, 12, 18), 1.8311))  # SPY 2026-12-18
    assert (r.dividend_in_hold, r.ex_dividend_date) == (False, dt.date(2026, 12, 18))
    assert not any("ex-dividend" in w for w in r.warnings)


def test_longer_hold_catches_a_later_ex_date():
    r = short(dividend=DividendInfo(TUE, 0.5), sessions=2)
    assert r.dividend_in_hold is True


def test_ex_dividend_without_an_amount_still_warns():
    r = short(dividend=DividendInfo(MON, None))
    assert r.dividend_owed is None
    assert any("amount IBKR didn't report" in w for w in r.warnings)


@pytest.mark.parametrize(
    ("borrow", "text"),
    [(BorrowInfo(2.0, None), "hard to borrow"), (BorrowInfo(1.0, 0), "no shares to borrow")],
)
def test_tight_borrow_warns_but_does_not_change_the_verdict(borrow, text):
    r = short(borrow=borrow)
    assert any(text in w for w in r.warnings)
    assert r.verdict == "CARRY"


def test_short_larger_than_the_shares_available_warns():
    r = short(borrow=BorrowInfo(3.0, 5))
    assert any("only 5 shares available to borrow, fewer than the 10 short" in w for w in r.warnings)


def test_unchecked_dividend_and_borrow_are_said_not_passed():
    r = short(dividend=None, borrow=None)
    assert r.dividend_checked is False and r.borrow_status is None
    assert any("ex-dividend not checked" in w for w in r.warnings)
    assert any("borrow not checked" in w for w in r.warnings)


def test_longs_get_no_short_side_fields_or_warnings():
    r = check_carry("XYZ", 100, 94, 120, IV_2, as_of=FRI, dividend=DividendInfo(MON, 1.0), borrow=BorrowInfo(1.0))
    assert (r.dividend_checked, r.dividend_in_hold, r.borrow_status) == (None, None, None)
    assert r.hold_end == MON
    assert r.warnings == []


def test_cli_short_reads_dividend_and_borrow_from_the_snapshot(monkeypatch):
    from tradekit.data import ibkr

    snap = ibkr.VolSnapshot(
        "SPY", 697.49, 0.3088, 0.1654, "live",
        dividends_known=True, ex_dividend_date=dt.date(2026, 11, 23), dividend_amount=1.8311,
        shortable=3.0, shortable_shares=6_093_985,
    )  # fmt: skip
    monkeypatch.setattr(ibkr, "fetch_vol_snapshot", lambda symbol: snap)
    monkeypatch.setattr(cli_mod, "now_et", lambda: dt.datetime(2026, 11, 20, 15, 0))
    result = run(["SPY", "--stop", "705", "--risk", "280", "--shares", "10"])
    assert result.exit_code == 0, result.output
    assert "Short side (hold through 2026-11-23)" in result.output
    assert "next ex-dividend 2026-11-23 (1.831/share), INSIDE the hold" in result.output
    assert "borrow available, 6,093,985 shares available" in result.output
    assert "18.31 on 10 shares" in result.output

    data = json.loads(run(["SPY", "--stop", "705", "--risk", "280", "--json"]).stdout)
    assert (data["as_of"], data["hold_end"], data["ex_dividend_date"]) == ("2026-11-20", "2026-11-23", "2026-11-23")
    assert (data["dividend_in_hold"], data["borrow_status"]) == (True, "available")


def test_cli_manual_short_says_dividend_and_borrow_were_not_checked():
    result = run(["XYZ", "--stop", "103", "--risk", "100", "--price", "100", "--iv", "0.3"])
    assert result.exit_code == 0, result.output
    assert "ex-dividend not checked; borrow not checked" in result.output
