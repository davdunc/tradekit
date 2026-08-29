"""Canonical, versioned document schema for trading reports.

Every report the system produces — the morning game plan and the daily report
card — is modeled here as a **single self-contained JSON document**. The shape
is deliberately document-oriented (NoSQL-friendly):

* Each document exposes a ``partition_key`` / ``sort_key`` pair via
  :meth:`ReportDocument.to_item`, mapping 1:1 onto a DynamoDB single-table
  item (``pk`` / ``sk``). This matches the existing ``falcon-stats`` DynamoDB
  store.
* Each document exposes :meth:`ReportDocument.archive_path`, a stable
  ``record_type/scope/date.json`` key for cold archival to object storage
  (S3 / compatible) without any transformation.

Because both surfaces (the Python tradekit package and the prose workflows)
serialize to this one schema, day-over-day results become directly comparable
and aggregatable instead of being re-parsed out of free-form prose.

Bump :data:`SCHEMA_VERSION` on any breaking field change so archived records
remain interpretable.
"""

import re
from enum import Enum
from typing import ClassVar, Self

from pydantic import BaseModel, Field, field_validator

from tradekit.reporting.grading import DISCIPLINE_MAX, Grade

SCHEMA_VERSION = "1.0"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class CovarianceStatus(str, Enum):
    """Z-score classification driving setup selection.

    NORMAL → Fashionably Late long candidate; EXTREME → Offsides short.
    """

    NORMAL = "NORMAL"
    EXTENDED = "EXTENDED"
    EXTREME = "EXTREME"


class AccountKind(str, Enum):
    LIVE = "LIVE"
    SIM = "SIM"


class MarketCycle(str, Enum):
    """The market-cycle call, using the Discipline Workshop's fixed vocabulary.

    Identifying the cycle is the guidelines' first and most critical selection
    step -- it decides whether the day is aggressive, defensive, or a sit-out.

    * ``HOT`` -- multiple premarket runners holding into the open with volume
      and money flow.
    * ``IDEAL_SHORT`` -- runners failing with clear confirmation, bouncing
      moderately into levels formed premarket.
    * ``SLOW`` -- one failed runner, no bounces, attention rotating to
      sub-dollar moves.
    """

    HOT = "HOT MARKET"
    IDEAL_SHORT = "IDEAL FOR SHORT"
    SLOW = "SLOW MARKET"


class RiskLevel(str, Enum):
    """Per-name risk tier shown in the Discipline Workshop watchlist table.

    Driven primarily by float: an ultra-low-float name is HIGH regardless of how
    clean the chart looks, because manipulation risk dominates the setup.
    """

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


# ── Shared building blocks ───────────────────────────────────────────────────


class MacroSignal(BaseModel):
    """One macro signal: raw value, regime label, and a one-line implication."""

    metric: str
    value: float | None = None
    label: str = ""
    note: str = ""


class MacroContext(BaseModel):
    """The six trading signals from the USMetrics macro block + regime synthesis."""

    vix: MacroSignal | None = None
    yield_curve: MacroSignal | None = None
    consumer: MacroSignal | None = None
    oil: MacroSignal | None = None
    stress: MacroSignal | None = None
    dollar: MacroSignal | None = None
    regime_bias: str = ""


class TradePlan(BaseModel):
    """A planned setup in the game plan (not yet executed).

    Stops/targets carry both price and R so the plan is comparable to the
    executed outcome on the same R axis.
    """

    ticker: str
    direction: Direction | None = None
    setup: str = ""
    bias: str = ""
    support: float | None = None
    resistance: float | None = None
    inflexion: float | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    r_planned: float | None = None
    z_score: float | None = None
    covariance: CovarianceStatus | None = None
    setup_grade: Grade | None = None
    intel_note: str = ""
    notes: str = ""

    # -- Discipline Workshop fields -----------------------------------------
    # The MIC plan format is "TICKER- L1 / L2 / L3, stop out X Float: Y Notes: ...",
    # which needs three *ordered* entry lines plus a float, and (for the expanded
    # watchlist table) price / sector / volume / risk tier. A single ``entry``
    # cannot express the three-line ladder, hence ``entry_lines``.
    entry_lines: list[float] = Field(default_factory=list)
    price: float | None = None
    sector: str = ""
    volume: float | None = None
    float_shares: float | None = None
    risk_level: RiskLevel | None = None

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    def mic_entry_lines(self) -> list[float]:
        """The ordered entry lines for the MIC plan format.

        Preference order, first non-empty wins:

        1. ``entry_lines`` -- set explicitly.
        2. ``support`` / ``inflexion`` / ``resistance`` -- the level triplet the
           screener already produces.
        3. ``entry`` alone -- a one-line plan, rendered as-is rather than padded
           out with invented levels.

        Ordering follows direction: a LONG ladder ascends into strength, a SHORT
        ladder descends into weakness. Returns ``[]`` when nothing is known so
        callers can refuse to render the line instead of posting a partial plan.
        """
        if self.entry_lines:
            return list(self.entry_lines)
        triplet = [v for v in (self.support, self.inflexion, self.resistance) if v is not None]
        if len(triplet) >= 2:
            return sorted(triplet, reverse=self.direction is Direction.SHORT)
        if self.entry is not None:
            return [self.entry]
        return []


class TradeRecord(BaseModel):
    """A single executed round-trip, graded against the canonical ladder."""

    ticker: str
    account: str
    account_kind: AccountKind | None = None
    direction: Direction | None = None
    setup: str = ""
    shares: float = 0
    entry: float | None = None
    stop: float | None = None
    exit: float | None = None
    realized_pnl: float = 0.0
    fees: float = 0.0
    r_multiple: float | None = None
    covariance: CovarianceStatus | None = None
    grade: Grade | None = None
    planned: bool = False
    verdict: str = ""

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class AccountPnL(BaseModel):
    """Per-account daily P&L, sourced verbatim from ``falcon-stats ingest``.

    The equity-shape fields (peak / trough / max drawdown with timestamps) are
    first-class because they reveal the day's shape that headline P&L hides.
    """

    account: str
    kind: AccountKind
    round_trips: int = 0
    wins: int = 0
    losses: int = 0
    realized: float = 0.0
    peak_equity: float | None = None
    peak_time: str | None = None
    trough_equity: float | None = None
    trough_time: str | None = None
    max_drawdown: float | None = None
    max_dd_time: str | None = None
    streak: str = ""
    avg_r: float | None = None

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return round(100 * self.wins / decided, 1) if decided else None


class DisciplineResult(BaseModel):
    """Serializable discipline score: the per-criterion breakdown is the record.

    ``total`` is persisted alongside ``met`` so downstream readers that lack the
    rubric definition can still rank days, while ``met`` keeps the score
    reproducible.
    """

    met: dict[str, bool] = Field(default_factory=dict)
    total: int = 0
    out_of: int = DISCIPLINE_MAX

    def as_label(self) -> str:
        return f"{self.total}/{self.out_of}"


# ── Documents ────────────────────────────────────────────────────────────────


class ReportDocument(BaseModel):
    """Base for all persisted report documents.

    Subclasses set :attr:`record_type` and may override :meth:`scope`. The
    base provides the NoSQL item mapping and the object-storage archive path.
    """

    record_type: ClassVar[str] = "DOCUMENT"

    schema_version: str = SCHEMA_VERSION
    date: str

    @field_validator("date")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        if not _ISO_DATE.match(v):
            raise ValueError(f"date must be ISO YYYY-MM-DD, got {v!r}")
        return v

    def scope(self) -> str:
        """Partition scope. Account-agnostic documents stay ``GLOBAL``."""
        return "GLOBAL"

    def partition_key(self) -> str:
        return f"{self.record_type}#{self.scope()}"

    def sort_key(self) -> str:
        return self.date

    def to_item(self) -> dict:
        """Flat NoSQL item: model fields plus ``pk`` / ``sk`` / ``record_type``.

        Returns JSON-native types. A DynamoDB adapter is responsible for any
        float→Decimal coercion; object-storage archival uses the dict as-is.
        """
        item = self.model_dump(mode="json")
        item["pk"] = self.partition_key()
        item["sk"] = self.sort_key()
        item["record_type"] = self.record_type
        return item

    def archive_path(self) -> str:
        """Stable object-storage key, e.g. ``dailycard/GLOBAL/2026-06-11.json``."""
        return f"{self.record_type.lower()}/{self.scope()}/{self.date}.json"

    @classmethod
    def from_item(cls, item: dict) -> Self:
        """Rebuild a document from its stored item, dropping the index keys.

        Typed as ``Self`` rather than ``ReportDocument`` so that
        ``GamePlanRecord.from_item(...)`` is statically known to be a
        ``GamePlanRecord``; callers would otherwise have to cast.
        """
        data = {k: v for k, v in item.items() if k not in ("pk", "sk", "record_type")}
        return cls.model_validate(data)


class GamePlanRecord(ReportDocument):
    """The morning game plan as a structured document."""

    record_type: ClassVar[str] = "GAMEPLAN"

    market_regime: str = ""
    one_percent_change: str = ""
    thesis_ticker: str = ""
    macro: MacroContext = Field(default_factory=MacroContext)
    fresh_news: list[TradePlan] = Field(default_factory=list)
    second_day: list[TradePlan] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)

    # -- Discipline Workshop fields -----------------------------------------
    # The MIC guidelines require a market-cycle call from a fixed vocabulary, a
    # single declared direction for the day, and a separate "top runners"
    # observation list (names with volume, whether or not they are being planned).
    market_cycle: MarketCycle | None = None
    bias: Direction | None = None
    top_runners: list[TradePlan] = Field(default_factory=list)

    def all_tickers(self) -> list[str]:
        """Deduplicated plan tickers, thesis first — DAS Market Viewer order."""
        ordered: list[str] = []
        if self.thesis_ticker:
            ordered.append(self.thesis_ticker.strip().upper())
        for plan in [*self.fresh_news, *self.second_day]:
            if plan.ticker not in ordered:
                ordered.append(plan.ticker)
        return ordered


class DailyReportCard(ReportDocument):
    """The end-of-day report card — one document covering both accounts."""

    record_type: ClassVar[str] = "DAILYCARD"

    market_regime: str = ""
    one_percent_result: str = ""
    headline: str = ""
    accounts: list[AccountPnL] = Field(default_factory=list)
    trades: list[TradeRecord] = Field(default_factory=list)
    discipline: DisciplineResult = Field(default_factory=DisciplineResult)
    patterns: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    monitored_not_traded: list[str] = Field(default_factory=list)
    behavioral_contract: str = ""

    def account(self, kind: AccountKind) -> AccountPnL | None:
        for a in self.accounts:
            if a.kind == kind:
                return a
        return None

    @property
    def combined_realized(self) -> float:
        return round(sum(a.realized for a in self.accounts), 2)
