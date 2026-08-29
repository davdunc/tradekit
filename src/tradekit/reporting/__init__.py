"""Canonical reporting layer: one schema, one grade ladder, one risk language.

**Role boundary (tradekit ↔ falcon).** falcon owns *deterministic* evaluation of
the data — e.g. ``falcon-stats ingest`` is the authoritative P&L / equity-curve
source. tradekit's job is the complement: turn *unstructured* inputs (news,
filings, analyst commentary, chart screenshots, narrative trade reviews) into
*structured, ordered* results. This package is where that structuring lands —
deterministic numerics from falcon are embedded verbatim (see
:class:`~tradekit.reporting.schema.AccountPnL`), and tradekit adds the graded,
ranked, comparable layer (setup grades, discipline, patterns, lessons) on top.
The documents here are the structured hand-off back to the falcon ecosystem.

This package is the single source of truth for *how trading results are
measured, graded, persisted, and compared*. It exists to eliminate the report
inconsistency that made day-over-day comparison unreliable:

* :mod:`grading` — one A/B/C/D/F ladder + one discipline rubric (was two ladders).
* :mod:`runits` — R-units as a first-class risk language (was dollars-only in code).
* :mod:`schema` — versioned, document-oriented records (game plan, daily card).
* :mod:`store` — NoSQL-native, object-storage-archivable persistence.
* :mod:`aggregate` / :mod:`render` — comparison views with one column set.

Note the deliberate split from the sibling :mod:`tradekit.reports` package:
``reports`` formats *ad-hoc scan/analysis output*; ``reporting`` owns the
*structured report-card domain* (the records that must stay comparable).
"""

from tradekit.reporting.aggregate import (
    DayRow,
    WeeklyRollup,
    multi_day_trend,
    weekly_rollup,
)
from tradekit.reporting.grading import (
    DISCIPLINE_MAX,
    DISCIPLINE_RUBRIC,
    DisciplineScore,
    Grade,
    average_grade,
    discipline_from_flags,
    grade_from_score,
)
from tradekit.reporting.ingest import (
    DEFAULT_ACCOUNT_KINDS,
    account_pnl_from_falcon,
    accounts_from_falcon,
    build_daily_card,
    default_account_kinds,
    ingest_daily,
    trade_from_dict,
)
from tradekit.reporting.render import (
    render_daily_card,
    render_game_plan,
    render_multi_day_trend,
    render_weekly,
)
from tradekit.reporting.runits import (
    RiskConfig,
    fmt_r,
    position_risk,
    r_multiple,
    target_for_r,
)
from tradekit.reporting.schema import (
    SCHEMA_VERSION,
    AccountKind,
    AccountPnL,
    CovarianceStatus,
    DailyReportCard,
    Direction,
    DisciplineResult,
    GamePlanRecord,
    MacroContext,
    MacroSignal,
    TradePlan,
    TradeRecord,
)
from tradekit.reporting.store import (
    DynamoReportStore,
    FileReportStore,
    ReportStore,
    default_store_root,
)

__all__ = [
    "SCHEMA_VERSION",
    # grading
    "Grade",
    "grade_from_score",
    "average_grade",
    "DisciplineScore",
    "discipline_from_flags",
    "DISCIPLINE_RUBRIC",
    "DISCIPLINE_MAX",
    # ingest
    "build_daily_card",
    "ingest_daily",
    "account_pnl_from_falcon",
    "accounts_from_falcon",
    "default_account_kinds",
    "trade_from_dict",
    "DEFAULT_ACCOUNT_KINDS",
    # runits
    "RiskConfig",
    "fmt_r",
    "r_multiple",
    "position_risk",
    "target_for_r",
    # schema
    "Direction",
    "CovarianceStatus",
    "AccountKind",
    "MacroSignal",
    "MacroContext",
    "TradePlan",
    "TradeRecord",
    "AccountPnL",
    "DisciplineResult",
    "GamePlanRecord",
    "DailyReportCard",
    # store
    "ReportStore",
    "FileReportStore",
    "DynamoReportStore",
    "default_store_root",
    # aggregate
    "DayRow",
    "WeeklyRollup",
    "multi_day_trend",
    "weekly_rollup",
    # render
    "render_daily_card",
    "render_weekly",
    "render_multi_day_trend",
    "render_game_plan",
]
