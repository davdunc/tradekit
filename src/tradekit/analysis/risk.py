"""Grade-based position sizing and exit planning (ASET risk-tool principles).

The rules, in the order a trade is checked:

1. **Grade sets allocation.** A setup's grade (A+ / A / B / C / D) sets what fraction of
   the *daily stop* this trade may risk: A+ 80%, A 30%, B 15%, C 5%, D 0%. D is the default
   and means no trade until the setup earns a higher grade.
2. **Risk = daily stop × allocation**, optionally capped by a per-symbol maximum.
3. **Distance = |entry − stop|.** Direction comes from the stop's side: a stop under the
   entry is a long, a stop above it is a short.
4. **Shares = floor(risk ÷ distance).** Notional does not enter into it.
5. **Targets are R-multiples of the distance.** An exit strategy splits the position into
   tranches. Each tranche has a share of the position and a target, given either as an R
   multiple or as a fraction of the distance from entry to a reference price such as VWAP.
   Shares go to tranches by largest remainder, so the tranches always add up to the position.
6. **Scenarios** assume that after tranche *k* fills, the rest is stopped at the original
   stop, with no trailing.
7. **Validation:** the grade is above D, the stop is on the correct side, and the position
   is at least one share. When an ATR is supplied, the stop must also be at least
   ``min_stop_atr`` × ATR away. These failures block clearance. An exit plan that can't reach
   2:1, a tranche that rounds to zero shares, or a T1 under 1R only warns.
8. **Clearance** needs every pre-trade checklist step plus a valid plan: "no math, no trade".

Pure functions only: no I/O and no market data. Dollar amounts are whatever the caller passes in.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

GRADE_ALLOCATION: dict[str, float] = {"A+": 0.80, "A": 0.30, "B": 0.15, "C": 0.05, "D": 0.0}

GRADE_DESCRIPTION: dict[str, str] = {
    "A+": "Rare. All variables aligned. Maximum concentration. Never force it.",
    "A": "Strong catalyst + confirmed setup + favorable regime.",
    "B": "Solid setup with most confirming variables. Standard playbook.",
    "C": "Skill building. Small risk, real execution. Process trade.",
    "D": "Default. No trade until it earns a higher grade.",
}

PRE_TRADE_CHECKLIST: tuple[str, ...] = (
    "Grade allocated (C / B / A / A+)",
    "Structural stop price set",
    "Entry trigger + condition defined",
    "Target projected with >= 2:1 R:R",
    "Share size calculated = risk / distance",
    "Execute exactly as the math says",
)

MIN_REWARD_R = 2.0


class RiskError(ValueError):
    """Inputs that cannot produce a trade plan."""


@dataclass(frozen=True)
class Tranche:
    pct: float  # share of the position, percent
    r: float | None = None  # target as a multiple of the stop distance
    ref: str | None = None  # or: name of a reference price (e.g. "vwap")
    frac: float | None = None  # ...and the fraction of entry→ref distance
    label: str | None = None


@dataclass(frozen=True)
class ExitStrategy:
    key: str
    name: str
    rule: str
    tranches: tuple[Tranche, ...] = ()  # empty = reference targets only (1R / 2R)
    refs: tuple[str, ...] = ()  # reference prices the strategy needs


STRATEGIES: dict[str, ExitStrategy] = {
    s.key: s
    for s in (
        ExitStrategy("standard", "Standard", "Targets 1:1 and 2:1 as reference, no scaling plan"),
        ExitStrategy(
            "spencer",
            "Spencer Scalp",
            "1/4 at 1R, 1/2 at 2R, 1/4 at 3R",
            (Tranche(25, r=1), Tranche(50, r=2), Tranche(25, r=3)),
        ),
        ExitStrategy(
            "rubberband",
            "Rubberband Scalp",
            "1/3 at 1/3 of the way to VWAP, 1/3 at 2/3, 1/3 at VWAP",
            (
                Tranche(100 / 3, ref="vwap", frac=1 / 3, label="1/3 distance"),
                Tranche(100 / 3, ref="vwap", frac=2 / 3, label="2/3 distance"),
                Tranche(100 / 3, ref="vwap", frac=1.0, label="VWAP"),
            ),
            refs=("vwap",),
        ),
        ExitStrategy(
            "fashionably-late",
            "Fashionably Late",
            "Full position at the measured move (stop→entry distance projected from entry, 1:1)",
            (Tranche(100, r=1, label="Measured Move"),),
        ),
    )
}


@dataclass
class TrancheRow:
    index: int
    label: str
    pct: float
    r: float
    price: float
    shares: int
    pnl: float


@dataclass
class Scenario:
    name: str
    pnl: float
    r: float


@dataclass
class TradePlan:
    grade: str
    allocation: float
    daily_stop: float
    risk_budget: float  # daily_stop × allocation, after any cap
    capped: bool
    direction: str | None  # "long" | "short"
    entry: float
    stop: float
    distance: float
    shares: int
    actual_risk: float
    target_1r: float
    target_2r: float
    strategy: str
    tranches: list[TrancheRow] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    max_r: float = 2.0
    checks: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return all(self.checks.values())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["valid"] = self.valid
        return d


def split_shares(total: int, pcts: list[float]) -> list[int]:
    """Distribute ``total`` shares by percentage; largest remainder keeps the sum exact."""
    raw = [total * p / 100 for p in pcts]
    out = [math.floor(v) for v in raw]
    rest = total - sum(out)
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - out[i], i), reverse=True)
    for i in order[:rest]:
        out[i] += 1
    return out


def plan_trade(
    grade: str,
    daily_stop: float,
    entry: float,
    stop: float,
    *,
    strategy: str = "standard",
    refs: dict[str, float] | None = None,
    max_risk: float | None = None,
    atr: float | None = None,
    min_stop_atr: float | None = None,
) -> TradePlan:
    """Build the full plan for one trade. Raises :class:`RiskError` on unusable inputs."""
    grade = grade.upper()
    if grade not in GRADE_ALLOCATION:
        raise RiskError(f"unknown grade {grade!r}; expected one of {', '.join(GRADE_ALLOCATION)}")
    if strategy not in STRATEGIES:
        raise RiskError(f"unknown strategy {strategy!r}; expected one of {', '.join(STRATEGIES)}")
    if daily_stop <= 0:
        raise RiskError("daily stop must be positive")
    if entry <= 0 or stop <= 0:
        raise RiskError("entry and stop must be positive prices")
    if max_risk is not None and max_risk <= 0:
        raise RiskError("max risk must be positive")

    st = STRATEGIES[strategy]
    refs = refs or {}
    alloc = GRADE_ALLOCATION[grade]
    budget = daily_stop * alloc
    capped = max_risk is not None and budget > max_risk
    if capped:
        budget = max_risk  # type: ignore[assignment]

    distance = abs(entry - stop)
    direction = None if distance == 0 else ("long" if stop < entry else "short")
    sign = 1 if direction == "long" else -1
    shares = math.floor(budget / distance) if distance > 0 and budget > 0 else 0

    plan = TradePlan(
        grade=grade,
        allocation=alloc,
        daily_stop=daily_stop,
        risk_budget=budget,
        capped=capped,
        direction=direction,
        entry=entry,
        stop=stop,
        distance=distance,
        shares=shares,
        actual_risk=shares * distance,
        target_1r=entry + sign * distance,
        target_2r=entry + sign * 2 * distance,
        strategy=strategy,
    )
    if capped:
        plan.warnings.append(f"risk capped at {max_risk:g} (grade allocation was {daily_stop * alloc:g})")

    plan.checks["grade_allocated"] = alloc > 0
    plan.checks["stop_structure_valid"] = direction is not None
    plan.checks["shares_at_least_1"] = shares >= 1

    if atr is not None and min_stop_atr is not None and atr > 0:
        floor = min_stop_atr * atr
        plan.checks["stop_outside_noise"] = distance >= floor
        if distance < floor:
            plan.warnings.append(
                f"stop distance {distance:.2f} is under {min_stop_atr:g}x ATR ({floor:.2f}); "
                "noise will take it out before the thesis is wrong"
            )

    if st.tranches and direction is not None and shares >= 1:
        for ref in st.refs:
            v = refs.get(ref)
            if v is None:
                raise RiskError(f"strategy {st.name!r} needs --{ref}")
            if sign * (v - entry) <= 0:
                side = "above" if direction == "long" else "below"
                raise RiskError(f"{ref} ({v:g}) must be {side} the entry for a {direction}")

        alloc_sh = split_shares(shares, [t.pct for t in st.tranches])
        for i, (t, sh) in enumerate(zip(st.tranches, alloc_sh, strict=True)):
            move = t.frac * abs(refs[t.ref] - entry) if t.ref else t.r * distance  # type: ignore[operator]
            plan.tranches.append(
                TrancheRow(
                    index=i + 1,
                    label=t.label or f"{move / distance:g}R",
                    pct=t.pct,
                    r=move / distance,
                    price=entry + sign * move,
                    shares=sh,
                    pnl=sh * move,
                )
            )
        plan.max_r = max(row.r for row in plan.tranches)

        realized, sold = 0.0, 0
        n = len(plan.tranches)
        for k in range(n + 1):
            if k:
                realized += plan.tranches[k - 1].pnl
                sold += plan.tranches[k - 1].shares
            net = realized - (shares - sold) * distance
            name = "Stop before T1" if k == 0 else ("All targets hit" if k == n else f"T{k} hit, rest stopped")
            plan.scenarios.append(Scenario(name, net, net / plan.actual_risk))

        if any(row.shares == 0 for row in plan.tranches):
            plan.warnings.append("position too small for this split: a tranche has 0 shares")
        if plan.tranches[0].r < 1:
            plan.warnings.append(f"T1 is below 1:1 ({plan.tranches[0].r:.2f}R); stop is wide for the target")

    # The tool marks a sub-2:1 exit plan as a warning, not a failure: Fashionably Late is a
    # deliberate 1:1 measured move.
    if plan.max_r < MIN_REWARD_R:
        plan.warnings.append(f"max reward is {plan.max_r:.2f}R, under the {MIN_REWARD_R:g}:1 minimum")
    return plan


def clearance(plan: TradePlan, checklist_done: int) -> tuple[str, str]:
    """Return (status, message), mirroring the tool's verdict panel."""
    if plan.grade == "D":
        return "NO TRADE", "Grade D = 0% allocation. Wait for an A/A+ setup."
    if not plan.valid:
        failed = ", ".join(k for k, ok in plan.checks.items() if not ok)
        return "NOT READY", f"Plan checks failed: {failed}."
    if checklist_done < len(PRE_TRADE_CHECKLIST):
        return "NOT READY", (
            f"Checklist {checklist_done}/{len(PRE_TRADE_CHECKLIST)}. "
            "All steps must be complete before the order goes out."
        )
    return "READY", "All variables aligned. Trade exactly as calculated, with no further adjustments."
