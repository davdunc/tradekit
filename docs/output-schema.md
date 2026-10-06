# tradekit Output Schema

Reference for the structured data files `tradekit` emits. These are tradekit's own
machine-readable outputs (snapshots it writes and later re-reads for diffs, plus debate
transcripts). Human-facing rendering — terminal tables, `REVIEW.md`, Notion — is produced
separately by the `reports/` package and the markdown renderers in `cli.py` and is **not** a
stable data contract.

> **Suite note:** these files are *not* consumed by `falcon-stats`. falcon-stats ingests
> broker execution data (DAS CSV / Cobra Excel), not tradekit output. For how the three
> falcon-suite components actually exchange data, see
> [falcon-suite-compatibility.md](falcon-suite-compatibility.md).
>
> **Format note:** all files below are written with `json.dumps(..., indent=2, default=str)`,
> i.e. a **single pretty-printed JSON document per file** — *not* line-delimited JSON (JSONL).
> Each file is one complete object/array. `default=str` means any non-JSON-native value
> (timestamps, numpy scalars, etc.) is coerced to its string form.

---

## 1. Group rotation snapshot

Sector / industry / country performance pulled from Finviz Elite. The primary weekly-review
input and the most-consumed output.

**Written by:** `tradekit groups` ([cli.py:1519](../src/tradekit/cli.py#L1519)) and
`tradekit weekly-review` ([cli.py:1716](../src/tradekit/cli.py#L1716))
**Path:** `$XDG_DATA_HOME/tradekit/groups_<YYYY-MM-DD>.json` (default `~/.local/share/tradekit/`)

### Top-level shape

```json
{
  "sector":   [ <GroupRecord>, ... ],
  "industry": [ <GroupRecord>, ... ],
  "country":  [ <GroupRecord>, ... ]
}
```

Keys present depend on the `--group` flag: `tradekit groups --group sector` writes only the
`"sector"` key, while `--group all` (default) and `weekly-review` write all three.
`sector` ≈ 11 GICS-style sectors, `industry` ≈ 144 sub-industries, `country` = international list.

### GroupRecord

Each element is one row of a Finviz Elite group export, normalized in
[finviz_elite.py:32](../src/tradekit/data/finviz_elite.py#L32).

| Field      | Type    | Description                                              |
|------------|---------|----------------------------------------------------------|
| `no`       | int     | Finviz row number (`No.`)                                |
| `name`     | string  | Group name (sector / industry / country)                 |
| `perf_w`   | float   | Performance, week, in **percent** (e.g. `2.41` = +2.41%) |
| `perf_m`   | float   | Performance, month, percent                              |
| `perf_q`   | float   | Performance, quarter, percent                            |
| `perf_h`   | float   | Performance, half-year, percent                          |
| `perf_y`   | float   | Performance, year, percent                               |
| `perf_ytd` | float   | Performance, year-to-date, percent                       |
| `rvol`     | float   | Relative volume (1.0 = average)                          |
| `change`   | float   | Day change, percent                                      |
| `stocks`   | int     | Number of constituent stocks in the group               |

Notes:
- Percentage fields are stored as **bare numbers** (the `%` sign is stripped on ingest);
  `_pct()` coerces blanks / `-` to `0.0`.
- The exact column set is governed by `DEFAULT_GROUP_COLS` and may not include every field
  above on every run — consumers should treat missing keys defensively.

### Diff derivation (not persisted)

`diff_groups()` ([finviz_elite.py:235](../src/tradekit/data/finviz_elite.py#L235)) compares two
snapshots and yields, per group, `{name, today, prior, delta}` sorted by `delta` descending.
This is computed on the fly for the `--diff` flag and the weekly review; it is rendered into
markdown, not written as its own JSON file.

---

## 2. Debate transcript

Full bull / bear / judge debate for a single ticker, persisted for replay and backtest.

**Written by:** `bull_bear_debate(..., persist=True)`
([debate.py:188](../src/tradekit/agents/debate.py#L188)) — invoked by `weekly-review --with-debates`
**Path:** `$XDG_DATA_HOME/tradekit/debates/<TICKER>_<TIMESTAMP>.json`
(timestamp slug = first 15 chars of the UTC ISO timestamp with `:` and `-` removed)

### DebateResult

Serialized from the `DebateResult` dataclass ([debate.py:90](../src/tradekit/agents/debate.py#L90))
via `asdict`.

| Field       | Type                  | Description                                          |
|-------------|-----------------------|------------------------------------------------------|
| `ticker`    | string                | e.g. `"NVDA"`                                         |
| `timestamp` | string (ISO 8601 UTC) | When the debate ran                                  |
| `context`   | object                | The structured signal context fed to the agents     |
| `bull`      | AgentCase             | Bull-side analyst output                             |
| `bear`      | AgentCase             | Bear-side analyst output                             |
| `judge`     | JudgeVerdict \| null  | Final verdict; `null` if a side failed to parse      |
| `error`     | string \| null        | Top-level error message, else `null`                 |

#### AgentCase (`bull`, `bear`)

| Field        | Type            | Description                                             |
|--------------|-----------------|---------------------------------------------------------|
| `role`       | string          | `"bull"` or `"bear"`                                     |
| `raw_output` | string          | Raw model text                                          |
| `parsed`     | object \| null  | Parsed JSON case (below), or `null` if parse failed     |
| `latency_ms` | int             | Inference latency                                       |
| `error`      | string \| null  | Per-side error, else `null`                             |

`parsed` (when present) is the analyst's structured case:

```json
{
  "thesis": "one-sentence directional thesis",
  "evidence": ["specific data point", "..."],
  "invalidation": "price level or signal that kills the thesis",
  "conviction": 0.0
}
```

#### JudgeVerdict (`judge`)

| Field           | Type            | Description                                           |
|-----------------|-----------------|-------------------------------------------------------|
| `verdict`       | string          | `"long"`, `"short"`, or `"skip"`                      |
| `confidence`    | float           | `0.0`–`1.0`                                           |
| `rationale`     | string          | Why this side won and why this confidence             |
| `key_levels`    | object          | `{ "entry": float\|null, "stop": float\|null, "target": float\|null }` |
| `stronger_side` | string          | `"bull"`, `"bear"`, or `"neither"`                    |
| `raw_output`    | string          | Raw judge model text                                  |
| `latency_ms`    | int             | Inference latency                                     |

### `context` object (typical fields)

The context is open-ended — richer is better. As built by `weekly-review`
([cli.py:1755](../src/tradekit/cli.py#L1755)) it contains:

`current_price`, `ah_price`, `ah_change_pct`, `industry`, `ticker_perf_week`,
`industry_perf_week`, `rs_spread_pp`, `atr`, `rsi`, `recent_5_bars` (list of OHLCV dicts),
`setup_note`.

---

## 3. Weekly review (markdown — not a data contract)

**Written by:** `tradekit weekly-review` ([cli.py:1806](../src/tradekit/cli.py#L1806))
**Path:** `~/.claude/MEMORY/WORK/<YYYYMMDD>_weekly-review/REVIEW.md` (override with `--out-dir`)

Editable markdown composed from the group snapshot (§1), per-ticker RS spreads, and optional
debate verdicts (§2). It also (re)writes the group snapshot in §1 as a side effect. Treat
`REVIEW.md` as a human deliverable; consume the underlying `groups_<DATE>.json` and debate
JSON for data.

---

## File locations at a glance

| Output                | Path                                                          | Format             |
|-----------------------|---------------------------------------------------------------|--------------------|
| Group rotation        | `$XDG_DATA_HOME/tradekit/groups_<YYYY-MM-DD>.json`            | pretty JSON object |
| Debate transcript     | `$XDG_DATA_HOME/tradekit/debates/<TICKER>_<TS>.json`          | pretty JSON object |
| Weekly review         | `~/.claude/MEMORY/WORK/<YYYYMMDD>_weekly-review/REVIEW.md`    | markdown           |

---

## Regime assessment

Per-dimension market regime. This is the contract LifeOS reads: the MorningGamePlan workflow runs
`tradekit regime --json` and renders its regime table and Notion `Regime` field from this document.
Intent and rules: [intents/market-regime.md](intents/market-regime.md).

**Written by:** `tradekit regime` (stdout with `--json`; stored unless `--no-save`)
**Path:** `$XDG_DATA_HOME/tradekit/regime/<as_of>/<assessment_id>.json`. Records are never rewritten;
new information produces a new `assessment_id`.

```json
{
  "schema_version": "1.0",
  "classifier_version": "1.0.0",
  "configuration_version": "2026-10-06.1-proposed",
  "configuration_status": "experimental | validated",
  "assessment_id": "16-hex hash of inputs + state + transition",
  "as_of": "YYYY-MM-DD",
  "scope": {"kind": "market", "benchmark": "SPY", "confirm": "QQQ", "timeframe": "daily_pre_session"},
  "state": {
    "direction": "up | down | neutral | unknown",
    "structure": "trend | range | transition | unknown",
    "volatility": "low | normal | high | extreme | unknown",
    "participation": "broad | narrow | mixed | unknown",
    "liquidity": "normal | impaired | unknown",
    "event_flags": [{"type": "fomc", "label": "FOMC minutes 13:00 CT"}],
    "data_quality": "valid | degraded | unusable"
  },
  "glance": {
    "headline": "🔁 RANGE DAY — fade the edges, trade catalysts",
    "chips": ["→ Neutral", "🔁 Range", "● Normal vol", "◐ Mixed", "⚠ QQQ up vs SPY neutral"],
    "trade": ["range edge fade", "day1 news ORB"],
    "careful": ["fashionably late long (structure is range)"],
    "off": [],
    "provisional": true
  },
  "evidence": {"direction": {...}, "structure": {...}, "volatility": {...}, "participation": {...},
               "confirm_QQQ": {"direction": "...", ...}},
  "conflicts": ["QQQ direction=neutral vs SPY direction=up"],
  "data_quality_reasons": ["participation_unknown", "liquidity_unknown(no_quote_data)"],
  "transition": {"previous_confirmed": ["up", "trend"], "candidate": ["up", "range"],
                 "candidate_sessions": 1, "confirmed": ["up", "trend"], "status": "stable | pending | confirmed"},
  "model_book": [{"id": "M3", "name": "range", "rule": "structure=range"},
                 {"id": "M5", "name": "divergence", "rule": "...", "overlay": true}],
  "playbooks": [{"playbook": "range_edge_fade", "decision": "eligible | conditional | blocked",
                 "reasons": ["all_conditions_met"]}],
  "provenance": {"source": "massive", "requests": 3, "grouped_session": "YYYY-MM-DD", "errors": [], "config_path": "..."},
  "note": "Regime describes conditions and gates playbook eligibility. It is never a trade signal."
}
```

**`glance` is what a human reads first.** Renderers (the game plan, Notion) lead with `headline`,
then `chips`, then Trade / Careful / Off, and put `evidence` underneath. `glance` is derived only from
the assessment's own fields, so it never says more than the evidence does.

Consumers must read `configuration_status`. While it is `experimental` the thresholds are uncalibrated
proposals and the read is provisional.
