"""The single source of truth for grades and the discipline rubric.

Before this module existed the codebase carried two *different* grade ladders:

* ``analysis/scoring.py`` graded setups **A/B/C/F** (no D), and
* the ``DailyReview`` workflow graded executed trades **A/B/C/D/F**.

The same letter therefore meant different things on the screening side and the
review side, which is exactly what made report cards impossible to compare.
This module defines **one** five-rung ladder and **one** discipline rubric so a
"B" means the same thing whether it grades a setup score or an executed trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Grade(str, Enum):
    """Canonical five-rung grade ladder, best to worst.

    The meaning is shared across surfaces:

    * **A** — clean, by-the-book (setup or execution).
    * **B** — right idea, minor issues.
    * **C** — acceptable but flawed (churn, early exits, weak setup).
    * **D** — wrong thesis or a real discipline slip.
    * **F** — revenge trade / averaging down / no plan.
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @property
    def rank(self) -> int:
        """0 (=A, best) … 4 (=F, worst). Lets grades be averaged/compared."""
        return _ORDER.index(self)


_ORDER = [Grade.A, Grade.B, Grade.C, Grade.D, Grade.F]

# Default score→grade thresholds on a 0–100 scale. A single scale keeps the
# screener's composite score and any other 0–100 metric on the same ladder.
DEFAULT_THRESHOLDS: dict[Grade, float] = {
    Grade.A: 80.0,
    Grade.B: 65.0,
    Grade.C: 50.0,
    Grade.D: 35.0,
    Grade.F: 0.0,
}


def grade_from_score(score: float, thresholds: dict[Grade, float] | None = None) -> Grade:
    """Map a 0–100 score onto the canonical ladder (inclusive lower bounds)."""
    th = thresholds or DEFAULT_THRESHOLDS
    for grade in _ORDER:
        if score >= th[grade]:
            return grade
    return Grade.F


def average_grade(grades: list[Grade]) -> Grade | None:
    """Average a list of grades by rank, rounding to the nearest rung.

    Returns ``None`` for an empty list so callers can render "—" rather than a
    misleading default grade.
    """
    if not grades:
        return None
    avg = round(sum(g.rank for g in grades) / len(grades))
    return _ORDER[max(0, min(len(_ORDER) - 1, avg))]


# ── Discipline rubric ────────────────────────────────────────────────────────
# One fixed rubric, summing to 10. This reconciles the DailyReview Step-4 rubric
# (7 criteria) with the Step-8 "account separation" criterion that was scored in
# practice but missing from the rubric — it is now a first-class criterion.


@dataclass(frozen=True)
class DisciplineCriterion:
    key: str
    description: str
    points: int


DISCIPLINE_RUBRIC: tuple[DisciplineCriterion, ...] = (
    DisciplineCriterion("followed_game_plan", "Traded the published game plan, not screen impulses", 2),
    DisciplineCriterion("playbook_setups_only", "Only playbook setups (Offsides / Fashionably Late)", 1),
    DisciplineCriterion("honored_stops", "Honored stops; no averaging down into losers", 2),
    DisciplineCriterion("no_revenge_trading", "No revenge trading (grinding a ticker after a loss)", 1),
    DisciplineCriterion("paused_after_losses", "Paused / reset after losses", 1),
    DisciplineCriterion("thesis_trade_live", "Took the thesis trade live with conviction sizing", 1),
    DisciplineCriterion("appropriate_sizing", "Conviction sizing, not scattered small lots", 1),
    DisciplineCriterion("account_separation", "LIVE activity served the plan, not a parallel impulse book", 1),
)

DISCIPLINE_MAX = sum(c.points for c in DISCIPLINE_RUBRIC)  # == 10
_RUBRIC_BY_KEY = {c.key: c for c in DISCIPLINE_RUBRIC}


@dataclass
class DisciplineScore:
    """A computed discipline score with a per-criterion breakdown.

    ``met`` maps each rubric key to whether it was satisfied. The total is the
    sum of points for satisfied criteria, so the number is reproducible from the
    breakdown instead of being eyeballed each day.
    """

    met: dict[str, bool] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(_RUBRIC_BY_KEY[k].points for k, v in self.met.items() if v and k in _RUBRIC_BY_KEY)

    @property
    def out_of(self) -> int:
        return DISCIPLINE_MAX

    def breakdown(self) -> list[tuple[DisciplineCriterion, bool]]:
        """Rubric in canonical order paired with whether each was met."""
        return [(c, self.met.get(c.key, False)) for c in DISCIPLINE_RUBRIC]

    def as_label(self) -> str:
        """Render as ``"7/10"``."""
        return f"{self.total}/{self.out_of}"


def discipline_from_flags(**flags: bool) -> DisciplineScore:
    """Build a :class:`DisciplineScore` from keyword criterion flags.

    Unknown keys are ignored so callers fail soft; known criteria default to
    ``False`` (not met) when omitted.
    """
    met = {c.key: bool(flags.get(c.key, False)) for c in DISCIPLINE_RUBRIC}
    return DisciplineScore(met=met)
