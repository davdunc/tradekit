"""Tests for grade-based position sizing (tradekit.analysis.risk)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from tradekit.analysis.risk import RiskError, clearance, plan_trade, split_shares
from tradekit.cli import cli


def test_grade_allocation_sets_risk_and_shares():
    p = plan_trade("A", 1000, 50.0, 49.0)
    assert p.risk_budget == pytest.approx(300)
    assert p.direction == "long"
    assert p.shares == 300
    assert p.target_1r == pytest.approx(51.0)
    assert p.target_2r == pytest.approx(52.0)


def test_shares_floor_and_actual_risk():
    p = plan_trade("B", 1000, 10.0, 10.70)  # 150 / 0.70 = 214.28
    assert p.direction == "short"
    assert p.shares == 214
    assert p.actual_risk == pytest.approx(149.8)
    assert p.target_2r == pytest.approx(8.6)


def test_grade_d_is_no_trade():
    p = plan_trade("D", 1000, 50, 49)
    assert p.shares == 0
    assert clearance(p, 6)[0] == "NO TRADE"


def test_max_risk_caps_the_allocation():
    # A+ is 80% of a $84 daily stop = $67.20, capped at a $28 per-symbol 1R.
    p = plan_trade("A+", 84, 20.0, 19.5, max_risk=28)
    assert p.capped
    assert p.risk_budget == 28
    assert p.shares == 56


def test_min_stop_atr_check():
    p = plan_trade("A", 1000, 50, 49.5, atr=0.5, min_stop_atr=1.5)
    assert p.checks["stop_outside_noise"] is False
    assert clearance(p, 6)[0] == "NOT READY"
    assert plan_trade("A", 1000, 50, 49.2, atr=0.5, min_stop_atr=1.5).checks["stop_outside_noise"]


def test_split_shares_largest_remainder_sums_exactly():
    assert split_shares(10, [25, 50, 25]) == [2, 5, 3]  # tie → later tranche, as in the tool
    for n in range(0, 50):
        assert sum(split_shares(n, [100 / 3] * 3)) == n


def test_spencer_tranches_and_scenarios():
    p = plan_trade("A", 1000, 50.0, 49.0, strategy="spencer")  # 300 sh, 1.00 risk/sh
    assert [t.shares for t in p.tranches] == [75, 150, 75]
    assert [t.price for t in p.tranches] == pytest.approx([51, 52, 53])
    assert p.max_r == 3
    nets = [s.pnl for s in p.scenarios]
    assert nets[0] == pytest.approx(-300)  # stopped before T1 = -1R
    assert nets[1] == pytest.approx(75 - 225)  # T1 hit, 225 stopped
    assert nets[-1] == pytest.approx(75 + 300 + 225)
    assert p.scenarios[-1].r == pytest.approx(2.0)


def test_rubberband_targets_scale_to_vwap_short():
    p = plan_trade("B", 1000, 30.0, 30.5, strategy="rubberband", refs={"vwap": 28.5})
    assert p.direction == "short"
    assert [t.price for t in p.tranches] == pytest.approx([29.5, 29.0, 28.5])
    assert p.tranches[-1].r == pytest.approx(3.0)


def test_rubberband_rejects_vwap_on_wrong_side():
    with pytest.raises(RiskError, match="must be above"):
        plan_trade("B", 1000, 30.0, 29.5, strategy="rubberband", refs={"vwap": 29.0})
    with pytest.raises(RiskError, match="needs --vwap"):
        plan_trade("B", 1000, 30.0, 29.5, strategy="rubberband")


def test_fashionably_late_is_one_r_and_warns_under_two():
    p = plan_trade("A", 1000, 50, 49, strategy="fashionably-late")
    assert len(p.tranches) == 1 and p.tranches[0].price == pytest.approx(51)
    assert any("under the 2:1" in w for w in p.warnings)
    assert clearance(p, 6)[0] == "READY"  # warning, not a block


def test_entry_equals_stop_fails_structure():
    p = plan_trade("A", 1000, 50, 50)
    assert p.direction is None
    assert not p.checks["stop_structure_valid"]


def test_checklist_gates_clearance():
    p = plan_trade("A", 1000, 50, 49)
    assert clearance(p, 5)[0] == "NOT READY"
    assert clearance(p, 6)[0] == "READY"


def test_cli_size_json():
    r = CliRunner().invoke(
        cli,
        [
            "size",
            "--grade",
            "a",
            "--daily-stop",
            "1000",
            "--entry",
            "50",
            "--stop",
            "49",
            "--strategy",
            "spencer",
            "--checklist",
            "6",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    d = json.loads(r.stdout)
    assert d["shares"] == 300
    assert d["clearance"]["status"] == "READY"


def test_cli_size_usage_error():
    r = CliRunner().invoke(
        cli,
        ["size", "--grade", "A", "--daily-stop", "1000", "--entry", "50", "--stop", "49", "--strategy", "rubberband"],
    )
    assert r.exit_code != 0
    assert "vwap" in r.output
