"""R-unit math and formatting — the canonical risk lingua franca.

The trading workflows mandate that *all* stops, targets, daily limits, and
recap numbers are expressed in **R-units** (multiples of the per-trade risk),
with the derived dollar amount shown in parentheses for readability
(e.g. ``"+2.3R ($644)"``). This module is the single place that owns that
math and formatting so every report — Python-generated or workflow-driven —
speaks the same language.

1R = the dollar risk taken on a single trade = ``abs(entry - stop) * shares``.
An R-*multiple* normalizes any P&L or price distance against that unit, which
is what makes outcomes comparable across tickers, share sizes, and accounts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    """Per-account risk parameters — the R-CONFIG block.

    Mirrors the ``R-CONFIG`` the workflows read from
    ``PREFERENCES.md → Trading Preferences → Risk Parameters`` so the Python
    layer and the prose workflows derive dollars from the *same* numbers.

    Attributes:
        r_dollars: Dollar value of 1R (the standard per-trade risk budget).
        daily_max_r: Daily loss limit expressed in R (stored positive).
        per_trade_max_r: Max risk allowed on a single trade, in R.
        max_trades: Hard cap on round-trips for the session, or ``None`` for no
            cap. A trade *count* limit is not expressible in R -- a day can sit
            inside its R budget while still churning dozens of round-trips, which
            is the overtrading failure mode the Discipline Workshop grades on. The
            cap therefore has to be its own number.
        account: Optional account label this config applies to.
    """

    r_dollars: float = 280.0
    daily_max_r: float = 3.0
    per_trade_max_r: float = 1.0
    max_trades: int | None = None
    account: str = ""

    def dollars(self, r_multiple: float) -> float:
        """Convert an R-multiple to its dollar value under this config."""
        return r_multiple * self.r_dollars


def r_multiple(pnl: float, risk_dollars: float) -> float | None:
    """Express a realized/unrealized P&L as a multiple of risk taken.

    Returns ``None`` when risk is unknown or zero — most manual DAS trades
    have no recorded ``planned_stop``, which makes R unreliable. Callers
    should surface "R unreliable" rather than fabricate a number, exactly as
    the DailyReview anti-pattern warns.
    """
    if not risk_dollars or risk_dollars <= 0:
        return None
    return round(pnl / risk_dollars, 2)


def risk_per_share(entry: float, stop: float) -> float:
    """Per-share risk (1R per share) = distance from entry to stop."""
    return abs(entry - stop)


def position_risk(entry: float, stop: float, shares: float) -> float:
    """Total dollar risk for a position = per-share risk * shares."""
    return risk_per_share(entry, stop) * abs(shares)


def target_for_r(entry: float, stop: float, r: float, *, long: bool = True) -> float:
    """Price that realizes a given R-multiple from entry/stop.

    Useful for turning an ``R:R`` plan into a concrete target price in the
    game plan instead of hard-coding dollar targets.
    """
    per_share = risk_per_share(entry, stop)
    return entry + per_share * r if long else entry - per_share * r


def fmt_r(r_multiple_value: float | None, config: RiskConfig | None = None) -> str:
    """Format an R-multiple as ``"+2.3R ($644)"``.

    When ``config`` is provided the derived dollar amount is appended in
    parentheses. When the R-multiple is ``None`` (risk unknown) the canonical
    ``"R n/a"`` token is returned so reports stay honest about it.
    """
    if r_multiple_value is None:
        return "R n/a"
    r_part = f"{r_multiple_value:+.1f}R"
    if config is None:
        return r_part
    return f"{r_part} (${config.dollars(r_multiple_value):+,.0f})"


def fmt_r_level(r: float, config: RiskConfig | None = None) -> str:
    """Format a non-signed R magnitude (e.g. a daily limit) as ``"3R ($840)"``."""
    r_part = f"{r:g}R"
    if config is None:
        return r_part
    return f"{r_part} (${config.dollars(r):,.0f})"
