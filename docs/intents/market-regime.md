# Market Regime Context Engine — Intent

`tradekit regime` describes observable market conditions **per dimension** and decides which configured
playbooks are eligible for further evaluation. LifeOS takes its regime read from this output instead of
labelling regimes by hand.

The design draws on SMB Capital's public teaching that trading conditions decide which trades have an edge
and how to manage them (trend versus range, broad versus rotational participation, volatility-driven
strategy changes, changing catalysts). **It does not implement an SMB taxonomy.** No verified public SMB
specification with fixed regime names, thresholds or a classification algorithm was found (operator
research, 2026-10-06). Nothing here should be described as an SMB classification.

> A market regime is the current combination of price behavior, participation, volatility and opportunity
> conditions that changes the suitability of a trader's playbooks.

The question it answers is not "what regime are we in?" but "what should change because of these conditions?"

## Outcomes

For each requested as-of date the engine produces:

- an assessment for each dimension, with the evidence behind it;
- explicit `unknown` where evidence is insufficient;
- confirmed versus pending transitions;
- playbook eligibility decisions with machine-readable reasons;
- a stored, reproducible record for replay and journal analysis.

**A regime classification is never a trade signal.** Eligibility is not permission to submit an order.

## State representation

| Dimension | Values |
|---|---|
| direction | `up` · `down` · `neutral` · `unknown` |
| structure | `trend` · `range` · `transition` · `unknown` |
| volatility | `low` · `normal` · `high` · `extreme` · `unknown` |
| participation | `broad` · `narrow` · `mixed` · `unknown` |
| liquidity | `normal` · `impaired` · `unknown` |
| event_flags | typed list (`{type, label, at}`) supplied by the caller |
| data_quality | `valid` · `degraded` · `unusable` |

Dimensions are independent. "Up, range, narrow" is a normal reading, not a contradiction. Market,
sector and symbol context stay separate: a symbol catalyst never overwrites the market assessment.

## v1 scope and features

v1 is a **pre-session, daily-timeframe** read built from completed sessions strictly **before** `as_of`
(premarket use, and historical replay through the same interface). Intraday regular-session
classification is out of scope for v1; premarket quotes are not mixed into these measurements.

Data comes from **Massive first**, at most three REST requests, self-capped at 10 req/s:

1. grouped daily aggregates for the last completed session (all-stock breadth plus the sector ETFs, in one call);
2. daily bars for the direction benchmark (default SPY);
3. daily bars for the confirmation benchmark (default QQQ).

| Feature | Definition | Window / units |
|---|---|---|
| `close_vs_sma` | (close − SMA_n) / ATR_14 on the benchmark | `direction.sma_window` sessions; ATR units |
| `sma_slope` | (SMA_n[t] − SMA_n[t−k]) / ATR_14 | `direction.slope_lag` sessions; ATR units |
| `efficiency_ratio` | \|C_t − C_{t−n}\| / Σ\|C_i − C_{i−1}\| | `structure.er_window` sessions; 0–1; undefined → unknown when the denominator is 0 |
| `hv_percentile` | percentile of today's HV20 (annualized close-to-close σ) in its trailing distribution | `volatility.lookback` sessions |
| `advancing_fraction` | share of filtered-universe names that closed above their open (v1 proxy, see below) | universe filter in config |
| `sector_agreement` | share of sector ETFs whose session change has the benchmark's sign | ETF list in config |

`advancing_fraction` uses each ticker's change versus its prior close. The grouped endpoint gives one
session only, so v1 compares close to open (`c > o`) as the per-name direction. That proxy is recorded in
the evidence, never hidden.

Optional evidence that may be attached but never overrides a dimension: dealer gamma (`tradekit gex`),
caller-supplied events.

## Classification rules

- Deterministic and configuration-driven: the same inputs, prior state and config give the same output.
- **All thresholds come from `config/regime.yaml`, versioned, each with a status (`experimental` |
  `validated`).** `config/regime.example.yaml` ships *proposed* experimental values for operator review.
- A missing operational config or a missing required key raises `RegimeConfigError`. No plausible-looking
  default is substituted.
- Any score is a documented heuristic, never a probability.

## Uncertainty and data quality

`unknown` is returned per dimension when its inputs are missing, stale or too short. `data_quality` is
`valid` when every input is present and the last bar is the expected prior session, `degraded` when some
dimensions are unknown, and `unusable` when the direction benchmark is unavailable. Reasons are listed.

## Transitions

Each run compares the assessment with stored history. A changed (direction, structure) pair is a
**candidate**. It becomes **confirmed** after `transitions.confirm_sessions` consecutive sessions agree. The
record keeps the previous confirmed state, the candidate, the candidate start date and the confirmation date.

## Playbook policy

A separate module evaluates configured playbook requirements and returns `eligible`, `conditional` or
`blocked`, each with reason codes. A regime may restrict a risk budget but never increase it.
Profitability by regime is not assumed: that needs evaluation results.

## Model-book adapter

`M1` trend up · `M2` trend down · `M3` range · `M4` reversal (requires a confirmed transition that flips
direction) · `M5` divergence overlay (index direction without participation) · `M6` event overlay.
M5 and M6 can coexist with M1–M4. Each candidate lists the rule that produced it.

## Output contract

`tradekit regime --json` emits one JSON document. See [output-schema.md §Regime assessment](../output-schema.md).
Assessments are stored at `$XDG_DATA_HOME/tradekit/regime/<as_of>/<assessment_id>.json` and never rewritten.

## Non-goals and safety

The engine does not submit orders, change broker settings, size positions, guarantee outcomes, infer
unavailable internals (quotes, order book) or use future observations. An LLM may explain an assessment but
may not invent evidence or change its fields.

## Acceptance

Historical and live runs share one code path · identical replay gives identical output · future bars cannot
change an earlier as-of · missing breadth → `participation: unknown` · missing quotes → `liquidity: unknown` ·
conflicting evidence stays visible · transition persistence is honoured · every playbook decision has reasons ·
config and schema versions are recorded.

## Evaluation (beyond v1)

1. Contract correctness: schema, determinism, no leakage, missing-data handling (unit tests).
2. Classification behavior: agreement with reviewed session annotations, transition latency, switching rate.
3. Trading usefulness: out-of-sample playbook results by regime (sample counts, costs, expectancy,
   drawdown, rejected opportunities), from falcon-trades history.
