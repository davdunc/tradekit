"""Tests for the ``tradekit cards`` command group."""

import json

import pytest
from click.testing import CliRunner

from tradekit.cli import cli
from tradekit.reporting import (
    AccountKind,
    AccountPnL,
    DailyReportCard,
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
        result = _run(["cards", "gameplan", "2026-08-31", "--format", "dw", "--store", str(store_root)])
        assert result.exit_code == 0
        assert "**Market Assessment:** HOT MARKET" in result.output

    def test_table_format_is_the_analyst_view(self, store_root):
        result = _run(["cards", "gameplan", "2026-08-31", "--format", "table", "--store", str(store_root)])
        assert result.exit_code == 0
        assert "| Ticker | Bias | Setup |" in result.output
        assert "stop out" not in result.output

    def test_json_format_emits_the_raw_item(self, store_root):
        result = _run(["cards", "gameplan", "2026-08-31", "--format", "json", "--store", str(store_root)])
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["record_type"] == "GAMEPLAN"
        assert payload["fresh_news"][0]["entry_lines"] == [17.5, 18.0, 18.5]

    def test_invalid_format_rejected(self, store_root):
        result = _run(["cards", "gameplan", "2026-08-31", "--format", "xml", "--store", str(store_root)])
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
        result = _run(["cards", "gameplan", "2026-08-31", "--scope", "NOPE", "--store", str(store_root)])
        assert result.exit_code == 1
        assert "scope NOPE" in _errtext(result)


class TestOutFile:
    def test_out_writes_clean_text_without_the_banner(self, store_root, tmp_path):
        dest = tmp_path / "posts" / "plan.md"
        result = _run(["cards", "gameplan", "2026-08-31", "--store", str(store_root), "--out", str(dest)])
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
        result = _run(["cards", "gameplan", "2026-08-31", "--store", str(store_root), "--r-dollars", "500"])
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


FALCON_STATS_FIXTURE = [
    {"account": "1RB16917", "round_trips": 22, "wins": 14, "losses": 8, "realized": -95.40, "streak": "5W"},
    {"account": "TR4425", "round_trips": 22, "wins": 12, "losses": 10, "realized": 60.65, "streak": "1L"},
]

NARRATIVE_FIXTURE = {
    "headline": "LIVE broke its own daily-loss cap.",
    "trades": [
        {
            "ticker": "COIN",
            "account": "1RB16917",
            "direction": "SHORT",
            "shares": 35,
            "realized_pnl": -131.20,
            "grade": "F",
            "verdict": "Traded past its own stated void time.",
        }
    ],
    "discipline": {"met": {"no_averaging_down": False}},
    "patterns": ["Averaging down into a loser, LIVE, past the void time."],
    "lessons": ["A stated void time has to function as a hard stop."],
    "behavioral_contract": "COIN is locked for the rest of this week.",
}


@pytest.fixture
def falcon_stats_file(tmp_path):
    path = tmp_path / "falcon.json"
    path.write_text(json.dumps(FALCON_STATS_FIXTURE))
    return path


@pytest.fixture
def narrative_file(tmp_path):
    path = tmp_path / "narrative.json"
    path.write_text(json.dumps(NARRATIVE_FIXTURE))
    return path


class TestCardsIngest:
    def test_ingest_persists_a_retrievable_card(self, tmp_path, falcon_stats_file, narrative_file):
        store_root = tmp_path / "store"
        result = _run(
            [
                "cards",
                "ingest",
                "--falcon-stats",
                str(falcon_stats_file),
                "--narrative",
                str(narrative_file),
                "2026-09-15",
                "--store",
                str(store_root),
            ]
        )
        assert result.exit_code == 0
        assert "discipline" in result.output.lower()

        store = FileReportStore(root=store_root)
        item = store.get("DAILYCARD", "2026-09-15")
        assert item is not None
        card = DailyReportCard.from_item(item)
        assert card.account(AccountKind.LIVE).realized == -95.40
        assert card.account(AccountKind.SIM).realized == 60.65
        assert card.trades[0].ticker == "COIN"
        assert card.behavioral_contract == "COIN is locked for the rest of this week."

    def test_ingest_without_narrative_still_persists_falcon_numbers(self, tmp_path, falcon_stats_file):
        store_root = tmp_path / "store"
        result = _run(
            ["cards", "ingest", "--falcon-stats", str(falcon_stats_file), "2026-09-15", "--store", str(store_root)]
        )
        assert result.exit_code == 0
        store = FileReportStore(root=store_root)
        card = DailyReportCard.from_item(store.get("DAILYCARD", "2026-09-15"))
        assert card.account(AccountKind.LIVE).realized == -95.40
        assert card.trades == []

    def test_missing_falcon_stats_file_is_a_usage_error(self, tmp_path):
        result = _run(["cards", "ingest", "--falcon-stats", str(tmp_path / "nope.json"), "2026-09-15"])
        assert result.exit_code != 0


@pytest.fixture
def two_day_store(tmp_path, falcon_stats_file, narrative_file):
    """A report store with daily cards on two consecutive dates."""
    store_root = tmp_path / "store"
    _run(
        [
            "cards",
            "ingest",
            "--falcon-stats",
            str(falcon_stats_file),
            "--narrative",
            str(narrative_file),
            "2026-09-14",
            "--store",
            str(store_root),
        ]
    )
    _run(
        [
            "cards",
            "ingest",
            "--falcon-stats",
            str(falcon_stats_file),
            "--narrative",
            str(narrative_file),
            "2026-09-15",
            "--store",
            str(store_root),
        ]
    )
    return store_root


class TestCardsTrend:
    def test_trend_lists_both_days_date_sorted(self, two_day_store):
        result = _run(["cards", "trend", "--store", str(two_day_store)])
        assert result.exit_code == 0
        assert result.output.index("2026-09-14") < result.output.index("2026-09-15")
        assert "$-95.40" in result.output
        assert "$+60.65" in result.output

    def test_since_filters_out_earlier_days(self, two_day_store):
        result = _run(["cards", "trend", "--store", str(two_day_store), "--since", "2026-09-15"])
        assert "2026-09-14" not in result.output
        assert "2026-09-15" in result.output

    def test_no_cards_in_range_is_a_named_error(self, tmp_path):
        result = _run(["cards", "trend", "--store", str(tmp_path / "empty")])
        assert result.exit_code == 1
        assert "cards ingest" in _errtext(result)

    def test_out_writes_the_same_table(self, two_day_store, tmp_path):
        dest = tmp_path / "trend.md"
        _run(["cards", "trend", "--store", str(two_day_store), "--out", str(dest)])
        assert "Multi-Day Trend" in dest.read_text()


class TestCardsPublish:
    def test_missing_card_is_a_named_error(self, tmp_path):
        result = _run(["cards", "publish", "2099-01-01", "--store", str(tmp_path)])
        assert result.exit_code == 1
        assert "cards ingest" in _errtext(result)

    def test_dry_run_prints_public_summary_and_calls_nothing_external(
        self, tmp_path, falcon_stats_file, narrative_file, monkeypatch
    ):
        store_root = tmp_path / "store"
        _run(
            [
                "cards",
                "ingest",
                "--falcon-stats",
                str(falcon_stats_file),
                "--narrative",
                str(narrative_file),
                "2026-09-15",
                "--store",
                str(store_root),
            ]
        )

        calls = []
        monkeypatch.setattr("subprocess.run", lambda *a, **k: calls.append((a, k)))

        result = _run(["cards", "publish", "2026-09-15", "--store", str(store_root), "--dry-run"])
        assert result.exit_code == 0
        assert calls == []
        # The public render, not the private one — no $ amounts, no behavioral contract.
        assert "TRADE REVIEW" in result.output
        assert "$" not in result.output
        assert "locked for the rest of this week" not in result.output

    def test_reviews_dir_writes_the_full_private_card(
        self, tmp_path, falcon_stats_file, narrative_file, monkeypatch
    ):
        store_root = tmp_path / "store"
        _run(
            [
                "cards",
                "ingest",
                "--falcon-stats",
                str(falcon_stats_file),
                "--narrative",
                str(narrative_file),
                "2026-09-15",
                "--store",
                str(store_root),
            ]
        )
        monkeypatch.setattr("subprocess.run", lambda *a, **k: None)
        reviews_dir = tmp_path / "reviews"

        result = _run(
            [
                "cards",
                "publish",
                "2026-09-15",
                "--store",
                str(store_root),
                "--reviews-dir",
                str(reviews_dir),
            ]
        )
        assert result.exit_code == 0
        private_text = (reviews_dir / "REVIEW-2026-09-15.md").read_text()
        # The private file DOES carry $ amounts and the behavioral contract —
        # only the public targets are stripped.
        assert "$-95.40" in private_text or "$-131.20" in private_text
        assert "locked for the rest of this week" in private_text

    def test_skip_flags_prevent_the_matching_publish_call(
        self, tmp_path, falcon_stats_file, narrative_file, monkeypatch
    ):
        store_root = tmp_path / "store"
        _run(
            [
                "cards",
                "ingest",
                "--falcon-stats",
                str(falcon_stats_file),
                "--narrative",
                str(narrative_file),
                "2026-09-15",
                "--store",
                str(store_root),
            ]
        )

        calls = []
        monkeypatch.setattr("subprocess.run", lambda *a, **k: calls.append(a[0]))

        result = _run(
            [
                "cards",
                "publish",
                "2026-09-15",
                "--store",
                str(store_root),
                "--skip-slack",
                "--skip-notion",
                "--notion-page-id",
                "fake-page-id",
            ]
        )
        assert result.exit_code == 0
        # Only the PNL Tracker call should have fired.
        assert len(calls) == 1
        assert "mcp__claude_ai_Notion__notion-create-pages" in calls[0]

    def test_notion_append_skipped_without_a_page_id_but_others_still_fire(
        self, tmp_path, falcon_stats_file, narrative_file, monkeypatch
    ):
        store_root = tmp_path / "store"
        _run(
            [
                "cards",
                "ingest",
                "--falcon-stats",
                str(falcon_stats_file),
                "--narrative",
                str(narrative_file),
                "2026-09-15",
                "--store",
                str(store_root),
            ]
        )

        calls = []
        monkeypatch.setattr("subprocess.run", lambda *a, **k: calls.append(a[0]))

        result = _run(["cards", "publish", "2026-09-15", "--store", str(store_root)])
        assert result.exit_code == 0
        assert "⚠" in result.output or "not given" in result.output
        # Slack + PNL Tracker fire; Notion is skipped for lack of a page id.
        assert len(calls) == 2
        joined = [" ".join(c) for c in calls]
        assert any("slack_send_message" in c for c in joined)
        assert any("notion-create-pages" in c for c in joined)
        assert not any("notion-update-page" in c for c in joined)
