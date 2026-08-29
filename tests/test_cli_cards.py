"""Tests for the ``tradekit cards`` command group."""

import json

import pytest
from click.testing import CliRunner

from tradekit.cli import cli
from tradekit.reporting import (
    Direction,
    FileReportStore,
    GamePlanRecord,
    MarketCycle,
    RiskLevel,
    TradePlan,
)


@pytest.fixture
def store_root(tmp_path):
    """A report store holding one game plan for 2026-08-31."""
    store = FileReportStore(root=tmp_path)
    store.put(
        GamePlanRecord(
            date="2026-08-31",
            market_cycle=MarketCycle.HOT,
            bias=Direction.LONG,
            thesis_ticker="AEO",
            fresh_news=[
                TradePlan(
                    ticker="AEO",
                    direction=Direction.LONG,
                    entry_lines=[17.50, 18.00, 18.50],
                    stop=17.00,
                    float_shares=3_260_000_000,
                    risk_level=RiskLevel.LOW,
                    notes="Earnings beat, holding premarket VWAP.",
                )
            ],
            rules=["Max 5 trades"],
        )
    )
    return tmp_path


def _run(args):
    return CliRunner().invoke(cli, args)


def _errtext(result) -> str:
    """Error text, tolerating Click versions that split or merge the streams."""
    try:
        return result.stderr or result.output
    except ValueError:  # stderr not separately captured
        return result.output


class TestCardsGameplan:
    def test_dw_is_the_default_format(self, store_root):
        result = _run(["cards", "gameplan", "2026-08-31", "--store", str(store_root)])
        assert result.exit_code == 0
        assert "AEO- 17.50 / 18.00 / 18.50, stop out 17.00 Float: 3.26B" in result.output
        assert "Top runners:" in result.output

    def test_explicit_dw_format(self, store_root):
        result = _run(
            ["cards", "gameplan", "2026-08-31", "--format", "dw", "--store", str(store_root)]
        )
        assert result.exit_code == 0
        assert "**Market Assessment:** HOT MARKET" in result.output

    def test_table_format_is_the_analyst_view(self, store_root):
        result = _run(
            ["cards", "gameplan", "2026-08-31", "--format", "table", "--store", str(store_root)]
        )
        assert result.exit_code == 0
        assert "| Ticker | Bias | Setup |" in result.output
        assert "stop out" not in result.output

    def test_json_format_emits_the_raw_item(self, store_root):
        result = _run(
            ["cards", "gameplan", "2026-08-31", "--format", "json", "--store", str(store_root)]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["record_type"] == "GAMEPLAN"
        assert payload["fresh_news"][0]["entry_lines"] == [17.5, 18.0, 18.5]

    def test_invalid_format_rejected(self, store_root):
        result = _run(
            ["cards", "gameplan", "2026-08-31", "--format", "xml", "--store", str(store_root)]
        )
        assert result.exit_code != 0

    def test_missing_plan_exits_nonzero_and_names_the_path(self, store_root):
        result = _run(["cards", "gameplan", "2099-01-01", "--store", str(store_root)])
        assert result.exit_code == 1
        assert "No game plan stored for 2099-01-01" in _errtext(result)

    def test_missing_plan_writes_nothing_to_stdout(self, store_root):
        # A failure must not leave half a document in a pipe.
        result = _run(["cards", "gameplan", "2099-01-01", "--store", str(store_root)])
        assert "Discipline Workshop Plan" not in result.stdout

    def test_unknown_scope_is_reported_not_silently_empty(self, store_root):
        result = _run(
            ["cards", "gameplan", "2026-08-31", "--scope", "NOPE", "--store", str(store_root)]
        )
        assert result.exit_code == 1
        assert "scope NOPE" in _errtext(result)


class TestOutFile:
    def test_out_writes_clean_text_without_the_banner(self, store_root, tmp_path):
        dest = tmp_path / "posts" / "plan.md"
        result = _run(
            ["cards", "gameplan", "2026-08-31", "--store", str(store_root), "--out", str(dest)]
        )
        assert result.exit_code == 0
        text = dest.read_text()
        # The file must be postable as-is: no ANSI escapes, no session banner.
        assert "\x1b[" not in text
        assert "ET —" not in text
        assert text.startswith("## Discipline Workshop Plan — 2026-08-31")

    def test_out_creates_parent_directories(self, store_root, tmp_path):
        dest = tmp_path / "a" / "b" / "plan.md"
        _run(["cards", "gameplan", "2026-08-31", "--store", str(store_root), "--out", str(dest)])
        assert dest.exists()

    def test_out_matches_stdout_rendering(self, store_root, tmp_path):
        dest = tmp_path / "plan.md"
        _run(["cards", "gameplan", "2026-08-31", "--store", str(store_root), "--out", str(dest)])
        piped = _run(["cards", "gameplan", "2026-08-31", "--store", str(store_root)])
        assert dest.read_text() in piped.output


class TestRiskOptions:
    def test_no_risk_block_when_no_options_given(self, store_root):
        result = _run(["cards", "gameplan", "2026-08-31", "--store", str(store_root)])
        assert "**Risk:**" not in result.output

    def test_max_trades_surfaces_the_cap(self, store_root):
        result = _run(
            [
                "cards",
                "gameplan",
                "2026-08-31",
                "--store",
                str(store_root),
                "--max-trades",
                "5",
            ]
        )
        assert "max 5 trades" in result.output

    def test_partial_risk_options_fill_from_defaults(self, store_root):
        # Only --r-dollars given; the R-based limits should still render.
        result = _run(
            ["cards", "gameplan", "2026-08-31", "--store", str(store_root), "--r-dollars", "500"]
        )
        assert "1R = $500" in result.output
        assert "daily stop 3R ($1,500)" in result.output

    def test_r_dollars_scales_the_daily_stop(self, store_root):
        result = _run(
            [
                "cards",
                "gameplan",
                "2026-08-31",
                "--store",
                str(store_root),
                "--r-dollars",
                "280",
                "--daily-max-r",
                "2",
            ]
        )
        assert "daily stop 2R ($560)" in result.output


class TestCardsGroup:
    def test_group_help_lists_gameplan(self):
        result = _run(["cards", "--help"])
        assert result.exit_code == 0
        assert "gameplan" in result.output

    def test_gameplan_help_documents_dw_format(self):
        result = _run(["cards", "gameplan", "--help"])
        assert result.exit_code == 0
        assert "Discipline Workshop" in result.output
