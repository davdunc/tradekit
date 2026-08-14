"""Ingest path: deterministic falcon stats + unstructured narrative → a card.

This is the loop-closing step. It honors the falcon ↔ tradekit boundary:

* **falcon** produces the deterministic numerics (``falcon-stats ingest`` —
  realized P&L, win/loss, peak/trough/max-drawdown with timestamps, streak,
  round-trip counts per account). Those are carried through **verbatim**.
* **tradekit** layers on the *structured judgment* derived from unstructured
  inputs (per-trade grades and verdicts, the discipline rubric, the day's
  patterns and lessons) and assembles a single comparable
  :class:`~tradekit.reporting.schema.DailyReportCard`.

The deterministic side arrives as a JSON contract (a list of per-account stat
objects, or a mapping keyed by account id). Field reading is **alias-tolerant**
so it adapts to falcon's exact key names without a brittle text parser — see
:data:`_FALCON_ALIASES`. The judgment side arrives as the ``narrative`` dict the
review workflow assembles.
"""

from __future__ import annotations

from tradekit.reporting.grading import DisciplineScore, Grade, discipline_from_flags
from tradekit.reporting.schema import (
    AccountKind,
    AccountPnL,
    CovarianceStatus,
    DailyReportCard,
    Direction,
    DisciplineResult,
    TradeRecord,
)

# Known DAS account ids → book kind. Override per call when ids differ.
DEFAULT_ACCOUNT_KINDS: dict[str, AccountKind] = {}
# Load account mappings from environment or config file instead of hardcoding

# Tolerant field aliases for the falcon per-account stat object. First match wins.
_FALCON_ALIASES: dict[str, tuple[str, ...]] = {
    "account": ("account", "account_id", "acct", "accid", "ACCID"),
    "kind": ("kind", "account_kind", "book", "type"),
    "round_trips": ("round_trips", "round_trip_count", "roundtrips", "trips", "rt", "n_round_trips"),
    "wins": ("wins", "win_count", "winners", "w"),
    "losses": ("losses", "loss_count", "losers", "l"),
    "win_rate": ("win_rate", "winrate", "win_pct"),
    "realized": ("realized", "realized_pnl", "realized_pl", "pnl", "net_pnl", "net"),
    "peak_equity": ("peak_equity", "peak", "peak_equity_value", "high_equity"),
    "peak_time": ("peak_time", "peak_at", "peak_equity_time"),
    "trough_equity": ("trough_equity", "trough", "min_equity", "low_equity"),
    "trough_time": ("trough_time", "trough_at", "min_equity_time"),
    "max_drawdown": ("max_drawdown", "max_dd", "maxdd", "drawdown"),
    "max_dd_time": ("max_dd_time", "max_drawdown_time", "dd_time", "maxdd_time"),
    "streak": ("streak",),
    "avg_r": ("avg_r", "average_r", "avg_r_multiple"),
}


def _first(d: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _g(d: dict, field: str, default=None):
    return _first(d, _FALCON_ALIASES[field], default)


def account_pnl_from_falcon(stat: dict, account_kinds: dict[str, AccountKind] | None = None) -> AccountPnL:
    """Map one falcon per-account DailyStats object to :class:`AccountPnL`.

    Numbers are taken verbatim. When falcon reports a win rate but not explicit
    win/loss counts, the counts are reconstructed from ``win_rate * round_trips``
    so the W/L column is still populated; explicit counts always win.
    """
    kinds = account_kinds or DEFAULT_ACCOUNT_KINDS
    account = str(_g(stat, "account", "")).strip()

    kind_raw = _g(stat, "kind")
    if kind_raw is not None:
        kind = AccountKind(str(kind_raw).upper())
    else:
        kind = kinds.get(account, AccountKind.LIVE)

    round_trips = int(_g(stat, "round_trips", 0) or 0)
    wins = _g(stat, "wins")
    losses = _g(stat, "losses")
    if wins is None and losses is None:
        win_rate = _g(stat, "win_rate")
        if win_rate is not None and round_trips:
            wins = round(float(win_rate) / 100.0 * round_trips)
            losses = round_trips - wins
    wins = int(wins or 0)
    losses = int(losses or 0)

    return AccountPnL(
        account=account,
        kind=kind,
        round_trips=round_trips,
        wins=wins,
        losses=losses,
        realized=float(_g(stat, "realized", 0.0) or 0.0),
        peak_equity=_opt_float(_g(stat, "peak_equity")),
        peak_time=_opt_str(_g(stat, "peak_time")),
        trough_equity=_opt_float(_g(stat, "trough_equity")),
        trough_time=_opt_str(_g(stat, "trough_time")),
        max_drawdown=_opt_float(_g(stat, "max_drawdown")),
        max_dd_time=_opt_str(_g(stat, "max_dd_time")),
        streak=str(_g(stat, "streak", "") or ""),
        avg_r=_opt_float(_g(stat, "avg_r")),
    )


def accounts_from_falcon(
    falcon_stats: list[dict] | dict,
    account_kinds: dict[str, AccountKind] | None = None,
) -> list[AccountPnL]:
    """Normalize falcon output (list of stats, or ``{account: stat}``) to rows."""
    if isinstance(falcon_stats, dict):
        # Mapping keyed by account id — inject the key as the account field.
        stats = []
        for acct, stat in falcon_stats.items():
            merged = {"account": acct, **stat} if "account" not in stat else dict(stat)
            stats.append(merged)
    else:
        stats = list(falcon_stats)
    return [account_pnl_from_falcon(s, account_kinds) for s in stats]


def trade_from_dict(d: dict, account_kinds: dict[str, AccountKind] | None = None) -> TradeRecord:
    """Build a graded :class:`TradeRecord` from the narrative's trade object."""
    kinds = account_kinds or DEFAULT_ACCOUNT_KINDS
    account = str(d.get("account", "")).strip()
    account_kind = d.get("account_kind") or d.get("kind")
    kind = AccountKind(str(account_kind).upper()) if account_kind else kinds.get(account)

    return TradeRecord(
        ticker=str(d.get("ticker", "")),
        account=account,
        account_kind=kind,
        direction=_opt_enum(Direction, d.get("direction")),
        setup=str(d.get("setup", "") or ""),
        shares=float(d.get("shares", 0) or 0),
        entry=_opt_float(d.get("entry")),
        stop=_opt_float(d.get("stop")),
        exit=_opt_float(d.get("exit")),
        realized_pnl=float(d.get("realized_pnl", d.get("realized", 0)) or 0),
        fees=float(d.get("fees", 0) or 0),
        r_multiple=_opt_float(d.get("r_multiple")),
        covariance=_opt_enum(CovarianceStatus, d.get("covariance")),
        grade=_opt_enum(Grade, d.get("grade")),
        planned=bool(d.get("planned", False)),
        verdict=str(d.get("verdict", "") or ""),
    )


def discipline_from_narrative(disc: dict | None) -> DisciplineResult:
    """Accept either rubric *flags* or an already-scored ``{met, total}`` block."""
    if not disc:
        return DisciplineResult()
    met = disc.get("met")
    if isinstance(met, dict):
        score = DisciplineScore(met={k: bool(v) for k, v in met.items()})
    else:
        # Treat the dict itself as criterion flags.
        score = discipline_from_flags(**{k: bool(v) for k, v in disc.items()})
    return DisciplineResult(met=score.met, total=score.total, out_of=score.out_of)


def build_daily_card(
    date: str,
    falcon_stats: list[dict] | dict,
    narrative: dict | None = None,
    *,
    account_kinds: dict[str, AccountKind] | None = None,
) -> DailyReportCard:
    """Assemble a :class:`DailyReportCard` from deterministic stats + narrative.

    Args:
        date: ISO ``YYYY-MM-DD``.
        falcon_stats: ``falcon-stats ingest`` output (deterministic, verbatim).
        narrative: structured judgment from the review workflow — keys
            ``trades``, ``discipline``, ``patterns``, ``lessons``, ``headline``,
            ``market_regime``, ``one_percent_result``, ``monitored_not_traded``,
            ``behavioral_contract``. All optional.
        account_kinds: override the account-id → LIVE/SIM mapping.
    """
    n = narrative or {}
    return DailyReportCard(
        date=date,
        market_regime=str(n.get("market_regime", "") or ""),
        one_percent_result=str(n.get("one_percent_result", "") or ""),
        headline=str(n.get("headline", "") or ""),
        accounts=accounts_from_falcon(falcon_stats, account_kinds),
        trades=[trade_from_dict(t, account_kinds) for t in n.get("trades", [])],
        discipline=discipline_from_narrative(n.get("discipline")),
        patterns=[str(p) for p in n.get("patterns", [])],
        lessons=[str(x) for x in n.get("lessons", [])],
        monitored_not_traded=[str(t).strip().upper() for t in n.get("monitored_not_traded", [])],
        behavioral_contract=str(n.get("behavioral_contract", "") or ""),
    )


def ingest_daily(
    date: str,
    falcon_stats: list[dict] | dict,
    narrative: dict | None,
    store,
    *,
    account_kinds: dict[str, AccountKind] | None = None,
) -> DailyReportCard:
    """Build the daily card and persist it to ``store``; returns the card."""
    card = build_daily_card(date, falcon_stats, narrative, account_kinds=account_kinds)
    store.put(card)
    return card


# ── small coercion helpers ───────────────────────────────────────────────────


def _opt_float(v):
    return None if v is None or v == "" else float(v)


def _opt_str(v):
    return None if v is None or v == "" else str(v)


def _opt_enum(enum_cls, v):
    if v is None or v == "":
        return None
    return enum_cls(str(v).upper())
