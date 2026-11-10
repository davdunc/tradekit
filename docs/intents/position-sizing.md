# Position Sizing and Exit Planning — Intent

`tradekit size` turns a graded setup plus a structural stop into a share count, R-multiple targets, an exit
plan, and a go/no-go verdict. The rules come from the **ASET risk tool** (SMB Training's position-size
calculator): *"Enter grade, daily stop, entry and stop — the tool calculates risk, share size and targets.
No math, no trade."* Decision record: [ADR 0003](../adr/0003-grade-based-position-sizing.md). JSON contract:
[output-schema.md § Position size plan](../output-schema.md#position-size-plan).

**A plan is never an order.** `READY` means the inputs are consistent and the checklist is complete. It
does not mean the trade is good. tradekit does not submit orders.

## Inputs

| Input | Flag | Required | Meaning |
|---|---|---|---|
| Grade | `--grade` | yes | `A+` `A` `B` `C` `D` (case-insensitive) |
| Daily stop | `--daily-stop` | yes | The maximum loss for the day, in dollars; must be > 0 |
| Entry | `--entry` | yes | Entry price; must be > 0 |
| Stop | `--stop` | yes | **Structural** stop price, which is a level on the chart and not a dollar amount. Its side sets the direction |
| Exit plan | `--strategy` | no | `standard` (default) · `spencer` · `rubberband` · `fashionably-late` |
| VWAP | `--vwap` | rubberband only | The reference price the rubberband plan scales into |
| Risk cap | `--max-risk` | no | Upper bound on dollars at risk, e.g. a per-symbol 1R limit |
| ATR | `--atr` | no | ATR **at entry**, on the timeframe of the hold. Turns on the stop-floor check |
| Stop floor | `--min-stop-atr` | no | Minimum stop distance in ATRs, default 1.5. Used only when `--atr` is given |
| Checklist | `--checklist` | no | Pre-trade checklist steps completed, 0–6 (default 0) |

## Rules

Each rule is enforced in `tradekit.analysis.risk` and covered by `tests/test_risk.py`.

| # | Rule | Definition |
|---|---|---|
| R1 | Grade sets allocation | Share of the daily stop: **A+ 80% · A 30% · B 15% · C 5% · D 0%**. D is the default state, and a D trade does not exist |
| R2 | Risk budget | `risk = daily_stop × allocation`. If `max_risk` is set and `risk > max_risk`, then `risk = max_risk`, `capped = true`, and a warning names the uncapped figure |
| R3 | Direction | `stop < entry` → long, `stop > entry` → short, `stop == entry` → none (invalid) |
| R4 | Distance | `distance = |entry − stop|`, per share |
| R5 | Size | `shares = floor(risk ÷ distance)`. **Notional (shares × price) is never an input**: risk is shares × distance |
| R6 | Actual risk | `actual_risk = shares × distance` (≤ risk, because of the floor) |
| R7 | Reference targets | `target_kR = entry ± k × distance` for k = 1, 2, with the sign set by direction |
| R8 | Tranche targets | A tranche targets either `r × distance` or `frac × |ref − entry|` (for example a third of the way to VWAP). The tranche's R is its move ÷ distance |
| R9 | Tranche shares | Largest-remainder split of `shares` by tranche percentage. Ties go to the **later** tranche. The sum always equals `shares` |
| R10 | Reference side | A reference price must lie on the target side of entry: above it for a long, below it for a short. Otherwise the plan is rejected, not adjusted |
| R11 | Scenarios | For k = 0..n: realized P&L of tranches 1..k, minus the remaining shares stopped at the **original** stop (no trailing). Expressed in dollars and in R (÷ actual risk) |
| R12 | Stop floor | With `--atr`: `distance ≥ min_stop_atr × atr`, or the stop sits inside noise and the plan is blocked |

## Exit plans

| Key | Name | Tranches |
|---|---|---|
| `standard` | Standard | none: the 1R/2R targets are reference only, with no scaling plan |
| `spencer` | Spencer Scalp | 25% at 1R · 50% at 2R · 25% at 3R |
| `rubberband` | Rubberband Scalp | ⅓ at ⅓ of entry→VWAP · ⅓ at ⅔ · ⅓ at VWAP (needs `--vwap`) |
| `fashionably-late` | Fashionably Late | 100% at the measured move: the stop→entry distance projected from entry (1:1) |

Adding a plan means adding an `ExitStrategy` to `STRATEGIES`. Tranche percentages must sum to 100.

## Validation and clearance

**Blocking checks.** Any one of these failing makes the plan invalid:

| Check | Fails when |
|---|---|
| `grade_allocated` | grade is D |
| `stop_structure_valid` | entry equals stop |
| `shares_at_least_1` | the risk budget can't buy one share at this distance |
| `stop_outside_noise` | (only with `--atr`) distance < `min_stop_atr × atr` |

**Warnings.** These are reported but never block:
- the best tranche is under 2:1 (Fashionably Late is 1:1 by design);
- T1 is under 1R;
- a tranche rounds to 0 shares;
- the risk was capped.

**Pre-trade checklist** (`PRE_TRADE_CHECKLIST`, six steps, in order):

1. Grade allocated (C / B / A / A+)
2. Structural stop price set
3. Entry trigger + condition defined
4. Target projected with ≥ 2:1 R:R
5. Share size calculated = risk ÷ distance
6. Execute exactly as the math says

**Clearance**, evaluated in this order:

| Status | When |
|---|---|
| `NO TRADE` | grade D |
| `NOT READY` | any blocking check failed, or checklist < 6 |
| `READY` | all checks pass and the checklist is 6/6. "Trade exactly as calculated, with no further adjustments" |

## Errors

Inputs that can't produce a plan raise `RiskError`, which the CLI shows as a usage error with a non-zero exit:

- an unknown grade or strategy;
- a daily stop, entry, stop, or max risk that is ≤ 0;
- a missing reference price (`--vwap` for rubberband);
- a reference price on the wrong side of entry.

## Non-goals

- **No market data.** The caller supplies prices and ATR; `risk.py` is pure and does no I/O.
- **No R-CONFIG.** Dollar figures are whatever the caller passes in. A caller that enforces per-account R
  (LifeOS: LIVE and SIM have separate 1R values) passes `--daily-stop` = 3R and `--max-risk` = 1R for the
  account the trade executes in.
- **No order routing,** no broker integration, and no persistence. The plan is printed or emitted as JSON only.
- **No grade inference.** tradekit does not grade the setup. The grade is the trader's call.

## Known limitation

At small daily stops, R1 and a per-symbol cap disagree. A+ is 80% of a 3R daily stop, which is 2.4R. The cap
is **opt-in by decision** (ADR 0003), so plain ASET math stays the default and the plan flags when a cap applied.
