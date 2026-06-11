"""Tests for the canonical reporting layer."""

import json

import pytest

from tradekit.reporting import (
    DISCIPLINE_MAX,
    AccountKind,
    AccountPnL,
    DailyReportCard,
    Direction,
    DisciplineResult,
    FileReportStore,
    GamePlanRecord,
    Grade,
    RiskConfig,
    TradePlan,
    TradeRecord,
    average_grade,
    discipline_from_flags,
    fmt_r,
    grade_from_score,
    multi_day_trend,
    position_risk,
    r_multiple,
    render_daily_card,
    render_multi_day_trend,
    render_weekly,
    target_for_r,
    weekly_rollup,
)
from tradekit.reporting.store import from_dynamo_item, to_dynamo_item


class TestRUnits:
    def test_r_multiple_basic(self):
        assert r_multiple(560, 280) == 2.0
        assert r_multiple(-140, 280) == -0.5

    def test_r_multiple_unknown_risk(self):
        assert r_multiple(100, 0) is None
        assert r_multiple(100, -5) is None

    def test_position_risk(self):
        assert position_risk(entry=10.0, stop=9.5, shares=100) == pytest.approx(50.0)

    def test_target_for_r(self):
        # 2R long from entry 10 / stop 9.5 (0.5 risk) → 10 + 1.0 = 11.0
        assert target_for_r(10.0, 9.5, 2, long=True) == pytest.approx(11.0)
        assert target_for_r(10.0, 9.5, 2, long=False) == pytest.approx(9.0)

    def test_fmt_r_with_and_without_config(self):
        cfg = RiskConfig(r_dollars=280)
        assert fmt_r(2.3, cfg) == "+2.3R ($+644)"
        assert fmt_r(2.3) == "+2.3R"
        assert fmt_r(None) == "R n/a"


class TestGrading:
    def test_grade_ladder_has_five_rungs(self):
        assert [g.value for g in Grade] == ["A", "B", "C", "D", "F"]

    def test_grade_from_score_includes_d(self):
        assert grade_from_score(85) is Grade.A
        assert grade_from_score(70) is Grade.B
        assert grade_from_score(55) is Grade.C
        assert grade_from_score(40) is Grade.D
        assert grade_from_score(10) is Grade.F

    def test_average_grade(self):
        assert average_grade([Grade.A, Grade.C]) is Grade.B
        assert average_grade([]) is None

    def test_discipline_rubric_sums_to_ten(self):
        assert DISCIPLINE_MAX == 10

    def test_discipline_from_flags(self):
        score = discipline_from_flags(
            followed_game_plan=True,
            playbook_setups_only=True,
            honored_stops=True,
            account_separation=True,
        )
        # 2 + 1 + 2 + 1 = 6
        assert score.total == 6
        assert score.out_of == 10
        assert score.as_label() == "6/10"

    def test_discipline_ignores_unknown_keys(self):
        score = discipline_from_flags(nonexistent_criterion=True)
        assert score.total == 0


class TestSchema:
    def test_ticker_uppercased(self):
        t = TradeRecord(ticker=" arm ", account="1RB16917")
        assert t.ticker == "ARM"

    def test_bad_date_rejected(self):
        with pytest.raises(ValueError):
            DailyReportCard(date="06/11/2026")

    def test_item_roundtrip(self):
        card = _sample_card()
        item = card.to_item()
        assert item["pk"] == "DAILYCARD#GLOBAL"
        assert item["sk"] == "2026-06-11"
        assert item["record_type"] == "DAILYCARD"
        restored = DailyReportCard.from_item(item)
        assert restored.date == card.date
        assert restored.combined_realized == card.combined_realized

    def test_archive_path(self):
        card = _sample_card()
        assert card.archive_path() == "dailycard/GLOBAL/2026-06-11.json"

    def test_gameplan_ticker_order(self):
        plan = GamePlanRecord(
            date="2026-06-11",
            thesis_ticker="nvda",
            fresh_news=[TradePlan(ticker="ARM"), TradePlan(ticker="NVDA")],
            second_day=[TradePlan(ticker="AMD")],
        )
        assert plan.all_tickers() == ["NVDA", "ARM", "AMD"]

    def test_combined_realized(self):
        card = _sample_card()
        assert card.combined_realized == pytest.approx(120.0)


class TestStore:
    def test_put_get_query(self, tmp_path):
        store = FileReportStore(root=tmp_path)
        store.put(_sample_card("2026-06-09"))
        store.put(_sample_card("2026-06-10"))
        store.put(_sample_card("2026-06-11"))

        got = store.get("DAILYCARD", "2026-06-10")
        assert got is not None and got["sk"] == "2026-06-10"
        assert store.get("DAILYCARD", "2099-01-01") is None

        rows = store.query("DAILYCARD", since="2026-06-10", until="2026-06-11")
        assert [r["sk"] for r in rows] == ["2026-06-10", "2026-06-11"]

    def test_file_layout_mirrors_object_storage(self, tmp_path):
        store = FileReportStore(root=tmp_path)
        card = _sample_card()
        store.put(card)
        # File lives exactly where its archive key says — verbatim S3 mirror.
        assert (tmp_path / card.archive_path()).exists()

    def test_put_is_idempotent(self, tmp_path):
        store = FileReportStore(root=tmp_path)
        store.put(_sample_card())
        store.put(_sample_card())
        assert len(store.query("DAILYCARD")) == 1

    def test_export_jsonl(self, tmp_path):
        import io

        store = FileReportStore(root=tmp_path)
        store.put(_sample_card("2026-06-10"))
        store.put(_sample_card("2026-06-11"))
        buf = io.StringIO()
        n = store.export_jsonl(buf, "DAILYCARD")
        assert n == 2
        lines = [json.loads(line) for line in buf.getvalue().splitlines()]
        assert {line["sk"] for line in lines} == {"2026-06-10", "2026-06-11"}

    def test_dynamo_item_decimal_roundtrip(self):
        from decimal import Decimal

        item = to_dynamo_item(_sample_card())
        # Floats become Decimal for boto3 compatibility.
        assert isinstance(item["trades"][0]["r_multiple"], Decimal)
        # Round-trip back to native types: non-integral stays float, integral → int.
        native = from_dynamo_item(item)
        assert isinstance(native["trades"][0]["r_multiple"], float)  # 1.5
        assert native["trades"][0]["r_multiple"] == pytest.approx(1.5)
        restored = DailyReportCard.from_item(native)
        assert restored.combined_realized == pytest.approx(120.0)


class TestAggregateAndRender:
    def test_multi_day_trend(self):
        items = [_sample_card("2026-06-11").to_item(), _sample_card("2026-06-09").to_item()]
        rows = multi_day_trend(items)
        assert [r.date for r in rows] == ["2026-06-09", "2026-06-11"]
        assert rows[0].live_pnl == pytest.approx(100.0)
        assert rows[0].sim_pnl == pytest.approx(20.0)

    def test_weekly_rollup(self):
        items = [_sample_card(d).to_item() for d in ("2026-06-09", "2026-06-10")]
        roll = weekly_rollup(items)
        assert roll.live_pnl == pytest.approx(200.0)
        assert roll.sim_pnl == pytest.approx(40.0)
        assert roll.total_pnl == pytest.approx(240.0)
        assert roll.days == 2
        assert roll.best_trade is not None
        assert any(p.setup == "Fashionably Late" for p in roll.setup_performance)

    def test_render_daily_card_has_canonical_tables(self):
        out = render_daily_card(_sample_card(), RiskConfig(r_dollars=280))
        assert "P&L Summary — Both Accounts" in out
        assert "| **LIVE (1RB16917)** |" in out
        assert "| **SIM (TR4425)** |" in out
        assert "| **COMBINED** |" in out
        assert "Discipline Score: 6/10" in out

    def test_render_multi_day_columns_stable(self):
        rows = multi_day_trend([_sample_card().to_item()])
        out = render_multi_day_trend(rows)
        assert "| Date | LIVE P&L | LIVE RTs | SIM P&L | Discipline | Avg Grade | Key Pattern |" in out

    def test_render_weekly_reuses_trend_table(self):
        items = [_sample_card(d).to_item() for d in ("2026-06-09", "2026-06-10")]
        rows = multi_day_trend(items)
        out = render_weekly(weekly_rollup(items), rows)
        assert "Daily Breakdown" in out
        assert "Setup Performance" in out


def _sample_card(date: str = "2026-06-11") -> DailyReportCard:
    return DailyReportCard(
        date=date,
        market_regime="Trending",
        accounts=[
            AccountPnL(
                account="1RB16917",
                kind=AccountKind.LIVE,
                round_trips=3,
                wins=2,
                losses=1,
                realized=100.0,
                peak_equity=150.0,
                peak_time="10:15:00",
                trough_equity=-30.0,
                trough_time="09:45:00",
                max_drawdown=-30.0,
                max_dd_time="09:45:00",
                streak="2NW",
            ),
            AccountPnL(
                account="TR4425",
                kind=AccountKind.SIM,
                round_trips=2,
                wins=1,
                losses=1,
                realized=20.0,
            ),
        ],
        trades=[
            TradeRecord(
                ticker="NVDA",
                account="1RB16917",
                account_kind=AccountKind.LIVE,
                direction=Direction.LONG,
                setup="Fashionably Late",
                shares=100,
                realized_pnl=80.0,
                r_multiple=1.5,
                grade=Grade.A,
                verdict="Clean FL long, held to target",
            ),
            TradeRecord(
                ticker="ARM",
                account="1RB16917",
                account_kind=AccountKind.LIVE,
                direction=Direction.SHORT,
                setup="Offsides",
                shares=50,
                realized_pnl=20.0,
                r_multiple=0.3,
                grade=Grade.C,
                verdict="Cut early",
            ),
        ],
        discipline=DisciplineResult(
            met={
                "followed_game_plan": True,
                "playbook_setups_only": True,
                "honored_stops": True,
                "account_separation": True,
            },
            total=6,
        ),
        patterns=["Exited winners early"],
        lessons=["Let the thesis trade breathe to first target"],
        behavioral_contract="Hold the thesis trade to plan; no premature scaling.",
    )
