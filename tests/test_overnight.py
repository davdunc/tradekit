"""Overnight-risk check: the carry math, verdicts, warnings, and the CLI without a broker."""

import json
import math
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from tradekit import cli as cli_mod
from tradekit.analysis.overnight import OvernightError, check_carry, expected_move

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
