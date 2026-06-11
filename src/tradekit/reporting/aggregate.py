"""Aggregation over persisted report cards — the comparability layer.

Multi-day and weekly views read *structured records* from a
:class:`~tradekit.reporting.store.ReportStore` instead of re-parsing prose. The
canonical per-day row (:class:`DayRow`) has exactly one column set, so the
"Compare to prior days", "Multi-Day Trend", and weekly "Daily Breakdown" tables
— which historically drifted apart — are now the same shape everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradekit.reporting.grading import Grade, average_grade
from tradekit.reporting.schema import AccountKind, DailyReportCard

# The one canonical multi-day column set. Every comparison table renders these,
# in this order, so days line up no matter which workflow produced them.
DAY_COLUMNS: tuple[str, ...] = (
    "date",
    "live_pnl",
    "live_round_trips",
    "sim_pnl",
    "discipline",
    "avg_grade",
    "key_pattern",
)


@dataclass
class DayRow:
    """One comparable day, derived from a persisted :class:`DailyReportCard`."""

    date: str
    live_pnl: float = 0.0
    live_round_trips: int = 0
    sim_pnl: float = 0.0
    discipline: int = 0
    discipline_out_of: int = 10
    avg_grade: Grade | None = None
    key_pattern: str = ""


def day_row_from_card(card: DailyReportCard) -> DayRow:
    """Project a full card down to the canonical comparison row."""
    live = card.account(AccountKind.LIVE)
    sim = card.account(AccountKind.SIM)
    grades = [t.grade for t in card.trades if t.grade is not None]
    return DayRow(
        date=card.date,
        live_pnl=live.realized if live else 0.0,
        live_round_trips=live.round_trips if live else 0,
        sim_pnl=sim.realized if sim else 0.0,
        discipline=card.discipline.total,
        discipline_out_of=card.discipline.out_of,
        avg_grade=average_grade(grades),
        key_pattern=card.patterns[0] if card.patterns else "",
    )


def multi_day_trend(items: list[dict]) -> list[DayRow]:
    """Build date-sorted :class:`DayRow` list from raw store items."""
    rows = [day_row_from_card(DailyReportCard.from_item(it)) for it in items]
    rows.sort(key=lambda r: r.date)
    return rows


@dataclass
class SetupPerformance:
    setup: str
    times: int = 0
    wins: int = 0
    pnl: float = 0.0

    @property
    def win_rate(self) -> float | None:
        return round(100 * self.wins / self.times, 1) if self.times else None

    @property
    def avg_pnl(self) -> float | None:
        return round(self.pnl / self.times, 2) if self.times else None


@dataclass
class WeeklyRollup:
    """Aggregated weekly metrics computed from persisted cards."""

    live_pnl: float = 0.0
    sim_pnl: float = 0.0
    total_round_trips: int = 0
    wins: int = 0
    losses: int = 0
    avg_discipline: float | None = None
    discipline_out_of: int = 10
    best_trade: tuple[str, float] | None = None
    worst_trade: tuple[str, float] | None = None
    setup_performance: list[SetupPerformance] = field(default_factory=list)
    days: int = 0

    @property
    def total_pnl(self) -> float:
        return round(self.live_pnl + self.sim_pnl, 2)

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return round(100 * self.wins / decided, 1) if decided else None


def weekly_rollup(items: list[dict]) -> WeeklyRollup:
    """Aggregate a week (or any span) of cards into one comparable rollup."""
    cards = [DailyReportCard.from_item(it) for it in items]
    roll = WeeklyRollup(days=len(cards))
    disciplines: list[int] = []
    setups: dict[str, SetupPerformance] = {}

    for card in cards:
        live = card.account(AccountKind.LIVE)
        sim = card.account(AccountKind.SIM)
        if live:
            roll.live_pnl += live.realized
            roll.total_round_trips += live.round_trips
            roll.wins += live.wins
            roll.losses += live.losses
        if sim:
            roll.sim_pnl += sim.realized
        roll.discipline_out_of = card.discipline.out_of
        disciplines.append(card.discipline.total)

        for trade in card.trades:
            if roll.best_trade is None or trade.realized_pnl > roll.best_trade[1]:
                roll.best_trade = (trade.ticker, trade.realized_pnl)
            if roll.worst_trade is None or trade.realized_pnl < roll.worst_trade[1]:
                roll.worst_trade = (trade.ticker, trade.realized_pnl)
            if trade.setup:
                perf = setups.setdefault(trade.setup, SetupPerformance(setup=trade.setup))
                perf.times += 1
                perf.pnl += trade.realized_pnl
                if trade.realized_pnl > 0:
                    perf.wins += 1

    roll.live_pnl = round(roll.live_pnl, 2)
    roll.sim_pnl = round(roll.sim_pnl, 2)
    if disciplines:
        roll.avg_discipline = round(sum(disciplines) / len(disciplines), 1)
    roll.setup_performance = sorted(setups.values(), key=lambda p: p.pnl, reverse=True)
    return roll
