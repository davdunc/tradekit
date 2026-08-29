"""Render canonical documents to Markdown with one consistent column set.

These renderers are the *only* place report-card tables are formatted, so the
daily card, the multi-day trend, and the weekly rollup cannot drift apart the
way the hand-written workflow tables did. All money/risk passes through the
shared R-unit formatter so dollars always derive from the same R-CONFIG.
"""

from __future__ import annotations

from tradekit.reporting.aggregate import DayRow, WeeklyRollup
from tradekit.reporting.runits import RiskConfig, fmt_r
from tradekit.reporting.schema import (
    AccountKind,
    AccountPnL,
    DailyReportCard,
    GamePlanRecord,
    TradeRecord,
)


def _money(x: float | None) -> str:
    if x is None:
        return "—"
    return f"${x:+,.2f}"


def _grade(g) -> str:
    return g.value if g is not None else "—"


def _r(trade: TradeRecord, config: RiskConfig | None) -> str:
    """R column for a trade: explicit r_multiple if set, else 'R n/a'."""
    return fmt_r(trade.r_multiple, config)


def _account_row(a: AccountPnL, config: RiskConfig | None) -> str:
    wr = a.win_rate
    wr_str = f"{wr:.0f}% ({a.wins}W/{a.losses}L)" if wr is not None else "—"
    peak = f"{_money(a.peak_equity)} @ {a.peak_time}" if a.peak_equity is not None else "—"
    trough = f"{_money(a.trough_equity)} @ {a.trough_time}" if a.trough_equity is not None else "—"
    maxdd = f"{_money(a.max_drawdown)} @ {a.max_dd_time}" if a.max_drawdown is not None else "—"
    # An unmapped account is labelled UNMAPPED, never LIVE. Reading a SIM book as
    # live risk is the failure this guards against; a visibly wrong label is fine,
    # a plausible wrong label is not.
    label = f"{a.kind.value} ({a.account})" if a.kind else f"UNMAPPED ({a.account})"
    return (
        f"| **{label}** | {a.round_trips} | {wr_str} "
        f"| {_money(a.realized)} | {peak} | {trough} | {maxdd} | {a.streak or '—'} |"
    )


def render_daily_card(card: DailyReportCard, config: RiskConfig | None = None) -> str:
    """Render the full daily report card (matches the DailyReview output format)."""
    lines: list[str] = [f"## Daily Report Card — {card.date}", ""]
    if card.market_regime:
        lines.append(f"*Regime: {card.market_regime}*")
        lines.append("")

    # P&L summary — both accounts + combined, identical columns every day.
    lines.append("### P&L Summary — Both Accounts")
    lines.append("")
    lines.append("| Account | Round-Trips | Win Rate | Realized | Peak Equity | Trough | Max DD | Streak |")
    lines.append("|---------|------------:|----------|---------:|-------------|--------|--------|--------|")
    rendered: list[int] = []
    for kind in (AccountKind.LIVE, AccountKind.SIM):
        a = card.account(kind)
        if a:
            rendered.append(id(a))
            lines.append(_account_row(a, config))
    # Accounts with no configured kind still get a row. Dropping them would hide
    # real P&L behind a missing config entry.
    for a in card.accounts:
        if id(a) not in rendered:
            lines.append(_account_row(a, config))
    decided_w = sum(a.wins for a in card.accounts)
    decided_l = sum(a.losses for a in card.accounts)
    rts = sum(a.round_trips for a in card.accounts)
    lines.append(
        f"| **COMBINED** | {rts} | {decided_w}W/{decided_l}L | {_money(card.combined_realized)} | — | — | — | — |"
    )
    lines.append("")
    if card.headline:
        lines.append(f"**Headline:** {card.headline}")
        lines.append("")

    # Trade-by-trade — R first, dollar derived.
    if card.trades:
        lines.append("### Trade-by-Trade Review")
        lines.append("")
        lines.append("| Ticker | Acct | Dir | Shares | Realized | R | Grade | Verdict |")
        lines.append("|--------|------|-----|-------:|---------:|---|:-----:|---------|")
        for t in card.trades:
            acct = t.account_kind.value if t.account_kind else t.account
            direction = t.direction.value if t.direction else "—"
            lines.append(
                f"| **{t.ticker}** | {acct} | {direction} | {t.shares:g} "
                f"| {_money(t.realized_pnl)} | {_r(t, config)} | {_grade(t.grade)} | {t.verdict} |"
            )
        lines.append("")

    if card.monitored_not_traded:
        lines.append("### Monitored but NOT Traded")
        lines.append(", ".join(card.monitored_not_traded))
        lines.append("")

    # Discipline — fixed rubric breakdown so the number is reproducible.
    lines.append(f"### Discipline Score: {card.discipline.as_label()}")
    if card.discipline.met:
        from tradekit.reporting.grading import DISCIPLINE_RUBRIC

        for c in DISCIPLINE_RUBRIC:
            mark = "✓" if card.discipline.met.get(c.key) else "✗"
            lines.append(f"- [{mark}] {c.description} ({c.points})")
    lines.append("")
    if card.one_percent_result:
        lines.append(f"**1% Change Result:** {card.one_percent_result}")
        lines.append("")

    if card.patterns:
        lines.append("### Patterns Identified")
        lines.extend(f"- {p}" for p in card.patterns)
        lines.append("")
    if card.lessons:
        lines.append("### Lessons")
        lines.extend(f"- {lesson}" for lesson in card.lessons)
        lines.append("")
    if card.behavioral_contract:
        lines.append("### Behavioral Contract for Tomorrow")
        lines.append(f"> *{card.behavioral_contract}*")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_multi_day_trend(rows: list[DayRow], title: str = "Multi-Day Trend") -> str:
    """The one canonical comparison table — used by daily AND weekly views."""
    lines = [f"### {title}", ""]
    lines.append("| Date | LIVE P&L | LIVE RTs | SIM P&L | Discipline | Avg Grade | Key Pattern |")
    lines.append("|------|---------:|---------:|--------:|:----------:|:---------:|-------------|")
    for r in rows:
        lines.append(
            f"| {r.date} | {_money(r.live_pnl)} | {r.live_round_trips} | {_money(r.sim_pnl)} "
            f"| {r.discipline}/{r.discipline_out_of} | {_grade(r.avg_grade)} | {r.key_pattern} |"
        )
    return "\n".join(lines) + "\n"


def render_weekly(rollup: WeeklyRollup, rows: list[DayRow], config: RiskConfig | None = None) -> str:
    """Render the weekly rollup, reusing the canonical multi-day table."""
    wr = rollup.win_rate
    avg_disc = f"{rollup.avg_discipline}/{rollup.discipline_out_of}" if rollup.avg_discipline is not None else "—"
    lines = [
        "## Weekly Review",
        "",
        "### P&L Summary",
        "",
        f"- **Live:** {_money(rollup.live_pnl)}  ",
        f"- **Sim:** {_money(rollup.sim_pnl)}  ",
        f"- **Total:** {_money(rollup.total_pnl)}  ",
        f"- **Round-Trips:** {rollup.total_round_trips}  ",
        f"- **Win Rate:** {wr:.0f}%  " if wr is not None else "- **Win Rate:** —  ",
        f"- **Avg Discipline:** {avg_disc}",
        "",
    ]
    lines.append(render_multi_day_trend(rows, title="Daily Breakdown"))

    if rollup.best_trade:
        lines.append(f"**Best Trade:** {rollup.best_trade[0]} ({_money(rollup.best_trade[1])})  ")
    if rollup.worst_trade:
        lines.append(f"**Worst Trade:** {rollup.worst_trade[0]} ({_money(rollup.worst_trade[1])})")
    lines.append("")

    if rollup.setup_performance:
        lines.append("### Setup Performance")
        lines.append("")
        lines.append("| Setup | Times | Win Rate | Avg P&L |")
        lines.append("|-------|------:|:--------:|--------:|")
        for p in rollup.setup_performance:
            wrp = f"{p.win_rate:.0f}%" if p.win_rate is not None else "—"
            lines.append(f"| {p.setup} | {p.times} | {wrp} | {_money(p.avg_pnl)} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_game_plan(plan: GamePlanRecord, config: RiskConfig | None = None) -> str:
    """Render the morning game plan summary block."""
    lines = [
        f"## Morning Game Plan — {plan.date}",
        "",
        f"**Market Regime:** {plan.market_regime or '—'}  ",
        f"**1% Change:** {plan.one_percent_change or '—'}  ",
        f"**Thesis Trade:** {plan.thesis_ticker or '—'}",
        "",
    ]
    m = plan.macro
    if m and any([m.vix, m.consumer, m.oil, m.regime_bias]):
        bits = []
        if m.vix:
            bits.append(f"VIX {m.vix.value} → {m.vix.label}")
        if m.consumer:
            bits.append(f"Consumer {m.consumer.value} → {m.consumer.label}")
        if m.oil:
            bits.append(f"Oil ${m.oil.value} → {m.oil.label}")
        lines.append("**Macro:** " + " | ".join(bits))
        if m.regime_bias:
            lines.append(f"> {m.regime_bias}")
        lines.append("")

    for title, plans in (("Fresh News", plan.fresh_news), ("Second Day Plays", plan.second_day)):
        if not plans:
            continue
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| Ticker | Bias | Setup | Z | Cov | Entry | Stop | Target | R |")
        lines.append("|--------|------|-------|--:|-----|------:|-----:|-------:|---|")
        for tp in plans:
            lines.append(
                f"| **{tp.ticker}** | {tp.bias or '—'} | {tp.setup or '—'} "
                f"| {tp.z_score if tp.z_score is not None else '—'} "
                f"| {tp.covariance.value if tp.covariance else '—'} "
                f"| {tp.entry if tp.entry is not None else '—'} "
                f"| {tp.stop if tp.stop is not None else '—'} "
                f"| {tp.target if tp.target is not None else '—'} "
                f"| {fmt_r(tp.r_planned, config)} |"
            )
        lines.append("")

    if plan.rules:
        lines.append("### Rules for Today")
        lines.extend(f"{i + 1}. {r}" for i, r in enumerate(plan.rules))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
