"""Tests for the canonical reporting layer."""

import json

import pytest

from tradekit.reporting import (
    DISCIPLINE_MAX,
    DW_CLOSING_LINE,
    AccountKind,
    AccountPnL,
    DailyReportCard,
    Direction,
    DisciplineResult,
    FileReportStore,
    GamePlanRecord,
    Grade,
    MarketCycle,
    RiskConfig,
    RiskLevel,
    TradePlan,
    TradeRecord,
    accounts_from_falcon,
    average_grade,
    build_daily_card,
    discipline_from_flags,
    fmt_r,
    grade_from_score,
    ingest_daily,
    multi_day_trend,
    position_risk,
    r_multiple,
    render_daily_card,
    render_dw_plan,
    render_game_plan,
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


class TestIngest:
    def _falcon(self):
        # Mimics falcon-stats output (alias-tolerant keys on purpose).
        return [
            {
                "account": "1RB16917",
                "round_trips": 4,
                "wins": 3,
                "losses": 1,
                "realized": 240.50,
                "peak": 300.0,
                "peak_at": "10:15:00",
                "trough": -45.0,
                "trough_at": "09:40:00",
                "max_dd": -60.0,
                "dd_time": "09:55:00",
                "streak": "2NW",
            },
            {
                "accid": "TR4425",  # different alias for the id
                "round_trip_count": 2,  # different alias for round_trips
                "win_rate": 50.0,  # no explicit wins/losses → reconstructed
                "net_pnl": -15.0,
            },
        ]

    def _narrative(self):
        return {
            "headline": "Clean LIVE day; SIM gave a bit back.",
            "market_regime": "Trending",
            "trades": [
                {
                    "ticker": "nvda",
                    "account": "1RB16917",
                    "direction": "long",
                    "setup": "Fashionably Late",
                    "shares": 100,
                    "realized_pnl": 220.0,
                    "r_multiple": 2.1,
                    "grade": "a",
                    "covariance": "normal",
                    "verdict": "Held to target",
                }
            ],
            "discipline": {  # rubric flags
                "followed_game_plan": True,
                "honored_stops": True,
                "account_separation": True,
            },
            "patterns": ["Disciplined account separation"],
            "lessons": ["Repeat the FL entry discipline"],
            "monitored_not_traded": ["amd", "arm"],
        }

    def test_accounts_from_falcon_verbatim_and_kinds(self):
        accts = accounts_from_falcon(self._falcon())
        live = next(a for a in accts if a.account == "1RB16917")
        sim = next(a for a in accts if a.account == "TR4425")
        assert live.kind is AccountKind.LIVE
        assert sim.kind is AccountKind.SIM  # inferred from known id
        # Deterministic numbers carried verbatim, incl. equity shape.
        assert live.realized == pytest.approx(240.50)
        assert live.max_drawdown == pytest.approx(-60.0)
        assert live.max_dd_time == "09:55:00"
        assert live.round_trips == 4

    def test_winrate_reconstructs_counts(self):
        accts = accounts_from_falcon(self._falcon())
        sim = next(a for a in accts if a.account == "TR4425")
        # 50% of 2 round-trips → 1W/1L.
        assert (sim.wins, sim.losses) == (1, 1)
        assert sim.realized == pytest.approx(-15.0)

    def test_falcon_mapping_form(self):
        mapping = {"1RB16917": {"round_trips": 1, "realized": 10.0}}
        accts = accounts_from_falcon(mapping)
        assert accts[0].account == "1RB16917" and accts[0].realized == pytest.approx(10.0)

    def test_build_daily_card_merges_deterministic_and_narrative(self):
        card = build_daily_card("2026-06-11", self._falcon(), self._narrative())
        assert card.combined_realized == pytest.approx(225.5)
        assert card.market_regime == "Trending"
        t = card.trades[0]
        assert t.ticker == "NVDA" and t.grade is Grade.A and t.direction is Direction.LONG
        assert t.account_kind is AccountKind.LIVE  # inferred from account id
        # discipline flags → reproducible total (2 + 2 + 1)
        assert card.discipline.total == 5
        assert card.monitored_not_traded == ["AMD", "ARM"]

    def test_build_daily_card_accepts_scored_discipline_block(self):
        narrative = {"discipline": {"met": {"honored_stops": True}, "total": 2}}
        card = build_daily_card("2026-06-11", self._falcon(), narrative)
        assert card.discipline.total == 2  # recomputed from met, not trusted blindly

    def test_ingest_daily_persists(self, tmp_path):
        store = FileReportStore(root=tmp_path)
        card = ingest_daily("2026-06-11", self._falcon(), self._narrative(), store)
        assert store.get("DAILYCARD", "2026-06-11") is not None
        # Round-trips through the store unchanged.
        reloaded = DailyReportCard.from_item(store.get("DAILYCARD", "2026-06-11"))
        assert reloaded.combined_realized == pytest.approx(card.combined_realized)

    def test_ingested_card_renders_canonical_tables(self):
        from tradekit.reporting import render_daily_card

        card = build_daily_card("2026-06-11", self._falcon(), self._narrative())
        out = render_daily_card(card, RiskConfig(r_dollars=280))
        assert "| **LIVE (1RB16917)** |" in out
        assert "| **SIM (TR4425)** |" in out
        assert "+2.1R" in out  # R-units rendered from the trade


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


# ── Discipline Workshop renderer ─────────────────────────────────────────────


def _dw_plan(**overrides) -> GamePlanRecord:
    """A game plan shaped like a real Discipline Workshop submission."""
    defaults = dict(
        date="2026-08-31",
        market_cycle=MarketCycle.HOT,
        bias=Direction.LONG,
        thesis_ticker="AEO",
        fresh_news=[
            TradePlan(
                ticker="aeo",
                direction=Direction.LONG,
                entry_lines=[17.50, 18.00, 18.50],
                stop=17.00,
                float_shares=3_260_000_000,
                price=17.76,
                sector="Retail",
                volume=4_000_000,
                risk_level=RiskLevel.LOW,
                notes="Earnings beat, 30% gap, holding above premarket VWAP.",
            ),
            TradePlan(
                ticker="BBLG",
                direction=Direction.LONG,
                entry_lines=[2.60, 2.80, 3.00],
                stop=2.40,
                float_shares=5_120_000,
                risk_level=RiskLevel.HIGH,
                notes="Ultra-low float, 35M volume. Scalp only.",
            ),
        ],
        second_day=[],
        rules=["Max 5 trades", "No chasing extensions"],
    )
    defaults.update(overrides)
    return GamePlanRecord(**defaults)


class TestMicEntryLines:
    def test_explicit_entry_lines_win(self):
        tp = TradePlan(ticker="AEO", entry_lines=[1.0, 2.0, 3.0], support=9.0, entry=8.0)
        assert tp.mic_entry_lines() == [1.0, 2.0, 3.0]

    def test_derived_from_level_triplet_ascending_for_long(self):
        tp = TradePlan(
            ticker="AEO",
            direction=Direction.LONG,
            support=17.5,
            inflexion=18.0,
            resistance=18.5,
        )
        assert tp.mic_entry_lines() == [17.5, 18.0, 18.5]

    def test_derived_triplet_descends_for_short(self):
        tp = TradePlan(
            ticker="XYZ",
            direction=Direction.SHORT,
            support=17.5,
            inflexion=18.0,
            resistance=18.5,
        )
        assert tp.mic_entry_lines() == [18.5, 18.0, 17.5]

    def test_single_entry_not_padded_with_invented_levels(self):
        tp = TradePlan(ticker="AEO", entry=17.5)
        assert tp.mic_entry_lines() == [17.5]

    def test_no_levels_returns_empty(self):
        assert TradePlan(ticker="AEO").mic_entry_lines() == []

    def test_lone_support_is_not_a_ladder(self):
        # One level out of the triplet is not enough to imply a three-line plan.
        tp = TradePlan(ticker="AEO", support=17.5)
        assert tp.mic_entry_lines() == []


class TestRenderDwPlan:
    def test_plan_line_matches_mic_format(self):
        out = render_dw_plan(_dw_plan())
        assert "AEO- 17.50 / 18.00 / 18.50, stop out 17.00 Float: 3.26B Notes: " in out
        assert "BBLG- 2.60 / 2.80 / 3.00, stop out 2.40 Float: 5.12M Notes: " in out

    def test_top_runners_block(self):
        out = render_dw_plan(_dw_plan())
        assert "Top runners:" in out
        assert "AEO-High volume Float: 3.26B" in out

    def test_explicit_top_runners_override_derived(self):
        plan = _dw_plan(top_runners=[TradePlan(ticker="CIFR", float_shares=2_050_000_000)])
        out = render_dw_plan(plan)
        assert "CIFR-High volume Float: 2.05B" in out
        assert "AEO-High volume Float" not in out

    def test_market_assessment_and_bias(self):
        out = render_dw_plan(_dw_plan())
        assert "**Market Assessment:** HOT MARKET" in out
        assert "**Bias:** LONG (first bounce)" in out

    def test_short_bias_names_death_candle_strategy(self):
        out = render_dw_plan(_dw_plan(bias=Direction.SHORT))
        assert "**Bias:** SHORT (death candle)" in out

    def test_market_regime_used_when_cycle_unset(self):
        out = render_dw_plan(_dw_plan(market_cycle=None, market_regime="Choppy"))
        assert "**Market Assessment:** Choppy" in out

    def test_closing_line_present_verbatim(self):
        assert DW_CLOSING_LINE in render_dw_plan(_dw_plan())

    def test_plan_without_stop_is_withheld_not_posted(self):
        plan = _dw_plan(
            fresh_news=[TradePlan(ticker="NVDA", entry_lines=[10.0, 11.0], stop=None)],
        )
        out = render_dw_plan(plan)
        assert "NVDA-" not in out
        assert "Withheld (no entry ladder or no stop): NVDA" in out

    def test_plan_without_levels_is_withheld(self):
        plan = _dw_plan(fresh_news=[TradePlan(ticker="NVDA", stop=9.0)])
        out = render_dw_plan(plan)
        assert "Withheld (no entry ladder or no stop): NVDA" in out

    def test_missing_float_renders_placeholder_not_zero(self):
        plan = _dw_plan(
            fresh_news=[TradePlan(ticker="NVDA", entry_lines=[10.0], stop=9.0)],
        )
        out = render_dw_plan(plan)
        assert "NVDA- 10.00, stop out 9.00 Float: —" in out

    def test_intel_note_used_when_notes_empty(self):
        plan = _dw_plan(
            fresh_news=[
                TradePlan(ticker="NVDA", entry_lines=[10.0], stop=9.0, intel_note="Gap and go")
            ],
        )
        assert "Notes: Gap and go" in render_dw_plan(plan)

    def test_rules_rendered(self):
        out = render_dw_plan(_dw_plan())
        assert "1. Max 5 trades" in out

    def test_risk_block_includes_trade_cap(self):
        cfg = RiskConfig(r_dollars=280, daily_max_r=3, per_trade_max_r=1, max_trades=5)
        out = render_dw_plan(_dw_plan(), cfg)
        assert "1R = $280" in out
        assert "daily stop 3R ($840)" in out
        assert "max 5 trades" in out

    def test_trade_cap_omitted_when_unset(self):
        risk_line = render_dw_plan(_dw_plan(), RiskConfig(r_dollars=280)).split("**Risk:**")[1]
        assert "trades" not in risk_line.split("\n")[0]

    def test_no_risk_block_without_config(self):
        assert "**Risk:**" not in render_dw_plan(_dw_plan())

    def test_ticker_normalized_to_upper(self):
        # 'aeo' was passed lowercase in the fixture.
        assert "aeo-" not in render_dw_plan(_dw_plan())

    def test_table_renderer_unchanged(self):
        # The analyst table and the DW format are separate surfaces.
        out = render_game_plan(_dw_plan())
        assert "## Morning Game Plan — 2026-08-31" in out
        assert "| Ticker | Bias | Setup |" in out
        assert DW_CLOSING_LINE not in out


class TestShareFormatting:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (8_440_000_000, "8.44B"),
            (40_150_000, "40.15M"),
            (5_120_000, "5.12M"),
            (1_000_000, "1M"),
            (1_500_000, "1.5M"),
            (65_000, "65K"),
            (900, "900"),
        ],
    )
    def test_share_counts(self, n, expected):
        from tradekit.reporting.render import _fmt_shares

        assert _fmt_shares(n) == expected

    def test_none_is_placeholder(self):
        from tradekit.reporting.render import _fmt_shares

        assert _fmt_shares(None) == "—"


class TestGamePlanRoundTrip:
    def test_dw_fields_survive_persistence(self, tmp_path):
        store = FileReportStore(root=tmp_path)
        store.put(_dw_plan())
        item = store.get("GAMEPLAN", "2026-08-31")
        assert item is not None
        restored = GamePlanRecord.from_item(item)
        assert restored.market_cycle is MarketCycle.HOT
        assert restored.bias is Direction.LONG
        assert restored.fresh_news[0].entry_lines == [17.50, 18.00, 18.50]
        assert restored.fresh_news[0].risk_level is RiskLevel.LOW
        assert restored.fresh_news[0].float_shares == 3_260_000_000
        # The rendered output is identical after a persistence round-trip.
        assert render_dw_plan(restored) == render_dw_plan(_dw_plan())
