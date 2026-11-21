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

Pure functions only: no I/O and no market data. IV and HV are annualized decimals (0.30 = 30%).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Literal

TRADING_DAYS = 252
DEFAULT_GAP_SIGMAS = 2.0
DEFAULT_EVENT_RATIO = 1.25

Verdict = Literal["CARRY", "REDUCE", "NO CARRY", "SIZE"]


class OvernightError(ValueError):
    """Inputs that cannot produce an overnight-risk check."""


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
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


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
) -> CarryCheck:
    """Apply the overnight-risk rules to one position. See the module docstring."""
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
        warnings=warnings,
    )
