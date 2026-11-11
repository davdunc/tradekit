# ADR 0003 — Position size comes from the setup grade and the structural stop

**Status:** accepted · **Date:** 2026-11-10

## Context

The ASET risk tool (SMB Training's position-size calculator) structures a trade before the order goes out.
The grade sets an allocation of the daily stop. The structural stop sets the distance. Size is risk ÷ distance,
and targets are R-multiples. A six-step checklist gates the order: "no math, no trade". tradekit had no sizing
code, so LifeOS sized by hand from its R-CONFIG.

## Decision

- `tradekit.analysis.risk` holds the rules as pure functions, and `tradekit size` exposes them (with `--json`).
- **Grade allocation of the daily stop:** A+ 80%, A 30%, B 15%, C 5%, D 0%. D is the default and means no trade.
- **Size:** shares = floor(daily stop × allocation ÷ |entry − stop|). Direction comes from the stop's side.
  Notional is never an input.
- **Exit plans as tranches:**
  - standard: 1R/2R reference only
  - Spencer Scalp: ¼ at 1R, ½ at 2R, ¼ at 3R
  - Rubberband: thirds of the entry→VWAP distance
  - Fashionably Late: all at the 1:1 measured move
  - Shares are split by largest remainder, and each plan reports stop-out scenarios after every tranche.
- **What blocks clearance:** grade D, an invalid stop side, a size under one share, an incomplete checklist, and
  (when `--atr` is given) a stop closer than `--min-stop-atr` × ATR.
- **What only warns:** a reward under 2:1, as Fashionably Late is by design, and a zero-share tranche.
- **Two local guards the source tool lacks:**
  - `--max-risk` caps the dollar risk, e.g. at a per-symbol 1R limit.
  - The ATR stop floor.
  - Both are optional, so the plain ASET math is still the default.

## Consequences

- With a small daily stop, the high grades exceed a per-symbol cap. A+ at 80% of a 3R daily stop is 2.4R. Callers
  that enforce a per-symbol limit must pass `--max-risk`, and the plan says when the cap applied.
- Allocations and exit plans are module constants, not config. Changing a percentage is a code change with a test.
