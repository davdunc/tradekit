# ADR 0002 — Market regime is described per dimension, not as one label

**Status:** accepted · **Date:** 2026-10-06

## Context

The 2026-10-06 LifeOS game plan carried two regime labels ("Melting Higher · Choppy") from a nine-option
Notion multi-select, while the MorningGamePlan workflow listed a different five (Trending / Ranging / Gap Day /
High Volatility / Choppy). Both lists mix different kinds of thing in one field: direction, structure, an
event and a volatility level. The old `tradekit regime` printed one SMA/RSI label per symbol. Operator research
found **no verified public SMB Capital regime taxonomy**. SMB's public teaching supports the separate questions
(trend vs range, broad vs rotational participation, volatility, catalysts), not a fixed list.

## Decision

- `tradekit regime` describes **direction, structure, volatility, participation, liquidity, events and data
  quality** as independent dimensions, with evidence and explicit `unknown`.
- Playbook eligibility is a separate policy step (`eligible` / `conditional` / `blocked` + reasons).
  M1–M6 are an adapter: M5 (divergence) and M6 (event) are overlays.
- Thresholds live only in a versioned config with a status. There is no built-in default: a missing config
  is an error. The shipped example is labelled proposed and experimental.
- Inputs come from Massive first (three bulk requests, ≤10 req/s, 403s recorded and not retried).
- LifeOS reads `tradekit regime --json` instead of labelling regimes by hand.
- The old table remains as `tradekit regime --legacy` and in the HTML report's `_collect_regime_data`.

## Consequences

- A day can read "up · range · narrow". The game plan shows it as a table, not one or two labels.
- Until the operator reviews and validates thresholds, every assessment says `experimental`.
- v1 is pre-session and daily-timeframe; intraday regular-session classification and quote-based liquidity
  are future work. The v1 breadth proxy is close-vs-open per name, recorded in the evidence.
