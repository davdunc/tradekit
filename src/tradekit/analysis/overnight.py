"""Overnight-risk check: can this position be carried past the close?

An intraday stop bounds the loss because price has to trade through it. Overnight it doesn't:
the next session can open past the stop, and the fill is the open, not the stop. The 12-week
broker audit (Mar 9 → May 29, 2026) put 42% of the period's loss in overnight and weekend holds,
and the single largest loss was one weekend carry, so a carry needs its own check.

The rules, in the order they are applied:

1. **Expected move.** The options market prices the move: ``price × IV × √(sessions ÷ 252)``
   is one standard deviation over ``sessions`` trading sessions, with IV annualized over 252
   trading days. A weekend is one session on this convention; overnight news over a weekend can
   still exceed it, so this is a floor on the risk, not a ceiling.
2. **Gap allowance.** Assume the next open can land ``gap_sigmas`` standard deviations away
   (default 2, about 95% of normal outcomes). The loss per share on a carry is therefore
   ``max(stop distance, gap_sigmas × σ)``, not the stop distance alone.
3. **Carry size.** ``floor(risk ÷ that loss per share)``: the most shares whose gap-adjusted
   loss stays inside the planned risk. Risk is the trade's 1R in currency.
4. **Verdict.** With a position given: ``CARRY`` if it fits, ``REDUCE`` to the carry size if it
   doesn't, ``NO CARRY`` if not even one share fits. Without one: ``SIZE`` and the carry size.
5. **Warnings** (never change the verdict):
   - the stop sits inside a 1σ move: an ordinary overnight move jumps it;
   - IV ÷ HV at or above ``event_ratio`` (default 1.25): the options market is pricing more
     movement than the stock has been delivering, which usually means an event (earnings, FDA,
     a deal). The rule is "nothing held into a print"; check the calendar.
6. **Short-side checks** (shorts only; warnings, never the verdict):
   - **Ex-dividend.** A short held at the close before the ex-date pays the dividend. The hold
     runs from ``as_of`` through ``ceil(sessions)`` weekdays later (``hold_end``); an ex-date in
     ``(as_of, hold_end]`` is in the hold. The price should open lower by about the dividend,
     which offsets it on the position, so it isn't added to the gap loss, but the cash is paid
     and a large or special dividend is worth knowing about before the close. Exchange holidays
     are not skipped: a holiday in the hold makes the window one session short.
   - **Borrow**, from IBKR's shortable level: above 2.5 shares are available, above 1.5 they
     must be located (hard to borrow), otherwise none. A carried short can be recalled and
     bought in at the open, so hard-to-borrow and unavailable both warn, as does a position
     larger than the shares IBKR shows available.
   - **Not checked is not clear.** With no dividend or borrow data for a short, the check says
     so rather than passing silently.

Pure functions only: no I/O and no market data. IV and HV are annualized decimals (0.30 = 30%).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Literal

TRADING_DAYS = 252
DEFAULT_GAP_SIGMAS = 2.0
DEFAULT_EVENT_RATIO = 1.25
#: IBKR shortable tick (236): above this, at least 1,000 shares are available to borrow.
SHORTABLE_AVAILABLE = 2.5
#: Above this (and at or below SHORTABLE_AVAILABLE), shares can be borrowed only if located.
SHORTABLE_LOCATE = 1.5

Verdict = Literal["CARRY", "REDUCE", "NO CARRY", "SIZE"]
BorrowStatus = Literal["available", "hard to borrow", "not available"]


class OvernightError(ValueError):
    """Inputs that cannot produce an overnight-risk check."""


@dataclass(frozen=True)
class DividendInfo:
    """The next dividend as the broker reports it. ``ex_date`` None means none is scheduled."""

    ex_date: dt.date | None
    amount: float | None


@dataclass(frozen=True)
class BorrowInfo:
    """IBKR's shortable level (tick 236) and, when given, the shares available to borrow."""

    shortable: float
    shares_available: int | None = None


def borrow_status(shortable: float) -> BorrowStatus:
    if shortable > SHORTABLE_AVAILABLE:
        return "available"
    if shortable > SHORTABLE_LOCATE:
        return "hard to borrow"
    return "not available"


def hold_end(as_of: dt.date, sessions: float) -> dt.date:
    """The last trading day of a hold of ``sessions`` sessions after ``as_of`` (weekends skipped)."""
    day, left = as_of, math.ceil(sessions)
    while left:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return day


def expected_move(price: float, iv: float, sessions: float = 1) -> float:
    """One standard deviation of price over ``sessions`` trading sessions, from annualized IV."""
    if price <= 0 or iv <= 0 or sessions <= 0:
        raise OvernightError("price, IV and sessions must all be positive")
    return price * iv * math.sqrt(sessions / TRADING_DAYS)


@dataclass(frozen=True)
class CarryCheck:
    symbol: str
    direction: Literal["long", "short"]
    price: float
    stop: float
    risk: float
    iv: float
    hv: float | None
    sessions: float
    gap_sigmas: float
    stop_distance: float
    one_sigma: float
    gap_move: float
    loss_per_share: float
    carry_shares: int
    shares: int | None
    gap_loss: float | None  # loss on ``shares`` if the open lands ``gap_sigmas`` σ against
    iv_hv_ratio: float | None
    verdict: Verdict
    # Hold window (needs ``as_of``) and the short-side checks. All None for a long.
    as_of: dt.date | None = None
    hold_end: dt.date | None = None
    dividend_checked: bool | None = None  # short only: False when there was no dividend data
    ex_dividend_date: dt.date | None = None
    dividend_per_share: float | None = None
    dividend_in_hold: bool | None = None  # None: not checked (long, no data, or no as_of)
    dividend_owed: float | None = None  # on ``shares``, when the ex-date is in the hold
    borrow_status: BorrowStatus | None = None
    shares_available: int | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v.isoformat() if isinstance(v, dt.date) else v for k, v in asdict(self).items()}


def check_carry(
    symbol: str,
    price: float,
    stop: float,
    risk: float,
    iv: float,
    *,
    hv: float | None = None,
    shares: int | None = None,
    sessions: float = 1,
    gap_sigmas: float = DEFAULT_GAP_SIGMAS,
    event_ratio: float = DEFAULT_EVENT_RATIO,
    as_of: dt.date | None = None,
    dividend: DividendInfo | None = None,
    borrow: BorrowInfo | None = None,
) -> CarryCheck:
    """Apply the overnight-risk rules to one position. See the module docstring.

    ``dividend`` and ``borrow`` are None when they weren't looked up; a short then gets a
    "not checked" warning. ``as_of`` is the trading date the carry starts from (today).
    """
    if risk <= 0:
        raise OvernightError("risk must be positive: it is the trade's 1R in currency")
    if stop == price:
        raise OvernightError("stop equals price: there is no invalidation level to carry against")
    if gap_sigmas < 0:
        raise OvernightError("gap_sigmas cannot be negative")
    if shares is not None and shares < 0:
        raise OvernightError("shares cannot be negative; the stop's side sets long or short")
    if hv is not None and hv <= 0:
        raise OvernightError("HV must be positive when given")

    one_sigma = expected_move(price, iv, sessions)
    direction: Literal["long", "short"] = "long" if stop < price else "short"
    stop_distance = abs(price - stop)
    gap_move = gap_sigmas * one_sigma
    loss_per_share = max(stop_distance, gap_move)
    carry_shares = math.floor(risk / loss_per_share + 1e-9)
    iv_hv_ratio = iv / hv if hv else None

    if shares is None:
        verdict: Verdict = "SIZE"
    elif carry_shares == 0:
        verdict = "NO CARRY"
    elif shares > carry_shares:
        verdict = "REDUCE"
    else:
        verdict = "CARRY"

    warnings = []
    if stop_distance < one_sigma:
        warnings.append(
            f"stop is {stop_distance:.2f} away, inside a 1σ move of {one_sigma:.2f}: "
            "an ordinary overnight move jumps it"
        )
    if iv_hv_ratio is not None and iv_hv_ratio >= event_ratio:
        warnings.append(
            f"IV/HV {iv_hv_ratio:.2f} ≥ {event_ratio:g}: options are pricing an event; "
            "check earnings and news before holding"
        )

    end = hold_end(as_of, sessions) if as_of else None
    short = _short_side(direction, shares, as_of, end, dividend, borrow, warnings)

    return CarryCheck(
        symbol=symbol.upper(),
        direction=direction,
        price=price,
        stop=stop,
        risk=risk,
        iv=iv,
        hv=hv,
        sessions=sessions,
        gap_sigmas=gap_sigmas,
        stop_distance=round(stop_distance, 4),
        one_sigma=round(one_sigma, 4),
        gap_move=round(gap_move, 4),
        loss_per_share=round(loss_per_share, 4),
        carry_shares=carry_shares,
        shares=shares,
        gap_loss=None if shares is None else round(shares * loss_per_share, 2),
        iv_hv_ratio=None if iv_hv_ratio is None else round(iv_hv_ratio, 4),
        verdict=verdict,
        as_of=as_of,
        hold_end=end,
        **short,
        warnings=warnings,
    )


def _short_side(
    direction: str,
    shares: int | None,
    as_of: dt.date | None,
    end: dt.date | None,
    dividend: DividendInfo | None,
    borrow: BorrowInfo | None,
    warnings: list[str],
) -> dict:
    """Ex-dividend and borrow fields for a short (appending warnings); empty for a long."""
    if direction != "short":
        return {}
    out: dict = {"dividend_checked": dividend is not None}

    if dividend is None:
        warnings.append("ex-dividend not checked: no dividend data for this short")
    else:
        out["ex_dividend_date"] = dividend.ex_date
        out["dividend_per_share"] = dividend.amount
        if as_of is not None and end is not None:
            in_hold = dividend.ex_date is not None and as_of < dividend.ex_date <= end
            out["dividend_in_hold"] = in_hold
            if in_hold:
                if dividend.amount is None:
                    owed, amount = None, "an amount IBKR didn't report"
                else:
                    owed = None if shares is None else round(shares * dividend.amount, 2)
                    amount = f"{dividend.amount:.4g}/share" + (
                        f", {owed:,.2f} on {shares:,} shares" if owed is not None else ""
                    )
                out["dividend_owed"] = owed
                warnings.append(
                    f"ex-dividend {dividend.ex_date.isoformat()} is inside the hold: the short pays {amount}; "
                    "the open should drop by about as much"
                )

    if borrow is None:
        warnings.append("borrow not checked: no shortability data for this short")
    else:
        status = borrow_status(borrow.shortable)
        out["borrow_status"] = status
        out["shares_available"] = borrow.shares_available
        if status == "not available":
            warnings.append("no shares to borrow: a carried short can be recalled and bought in at the open")
        elif status == "hard to borrow":
            warnings.append("hard to borrow (locate required): recall risk and higher fees on a carry")
        if borrow.shares_available is not None and shares is not None and shares > borrow.shares_available:
            warnings.append(
                f"only {borrow.shares_available:,} shares available to borrow, fewer than the {shares:,} short"
            )
    return out
