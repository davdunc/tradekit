# Falcon Suite — Component Map & Compatibility Matrix

The **Falcon suite** is three repositories that together cover an intraday
trading workflow: pre-market screening → analysis → game plan → post-trade
statistics. This document is the single source of truth for how the components
relate, what data contracts they share, and which versions are known to work
together.

Canonical location: this file lives in `tradekit/docs/` (tradekit is the
integration consumer of the shared contracts). A pointer copy sits at the
`falcon-suite/` working-directory root.

---

## Components

| Repo | Role | Runtime | Primary storage |
|------|------|---------|-----------------|
| [`davdunc/falcon`](https://github.com/davdunc/falcon) | Morning pipeline — scan gappers, pivots, grade, build game plan, post | AWS serverless (Step Functions + Lambda, "Arbol" passthrough) | DynamoDB `falcon-state` (provisioned; see gaps) |
| [`davdunc/tradekit`](https://github.com/davdunc/tradekit) | Analytical CLI — pre-market scans, S/R levels, indicators, scoring, group rotation, bull/bear debates, weekly review | Local Python 3.14 CLI | Local files (`$XDG_DATA_HOME/tradekit/`, falcon DB read-only) |
| [`davdunc/falcon-stats`](https://github.com/davdunc/falcon-stats) | Post-trade statistics — round-trips, R-multiples, win rate, expectancy, drawdown | Local Python 3.11 CLI | DynamoDB `falcon-trades` + parquet export (draft) |

---

## Shared data contracts

These are the **only** points where the components exchange data. (Notably,
falcon-stats does **not** consume tradekit's JSON output — a common
misconception. falcon-stats ingests broker execution data.)

| # | Contract | Producer | Consumer | Medium | Status |
|---|----------|----------|----------|--------|--------|
| C1 | **Screen runs** | falcon | tradekit | local SQLite `$XDG_DATA_HOME/falcon/falcon.db`, legacy `~/.falcon/falcon.db` (`screen_runs`, `screen_results`) | ✅ Satisfied as of falcon `shared/sqlite_store.py` (was a gap — see below) |
| C2 | **Trade_Review tree** | tradekit writes `news.json`; falcon-stats reads `Trades.csv`/`Orders.csv` | — | `$XDG_DATA_HOME/trade-review/<YYYY>/<MM>/<YYYY-MM-DD>/` | ⚠️ Shared parent dir only — **different files**, no direct data dependency |
| C3 | **Round-trip store** | falcon-stats | (parquet consumers / ML) | DynamoDB `falcon-trades`; parquet export | 🚧 Parquet export is draft (falcon-stats spec 001) |

### C1 — the falcon → tradekit screen-run contract

tradekit's `FalconReader` ([tradekit/data/falcon.py](../src/tradekit/data/falcon.py))
reads a local SQLite database to build watchlists for
`tradekit weekly-review --from-falcon`:

```sql
screen_runs(id INTEGER PK, strategy_name TEXT, executed_at TEXT)   -- naive-local ISO 8601
screen_results(run_id INTEGER FK, symbol TEXT, rank INTEGER)        -- rank = 1-based order
```

**History of the gap:** the published serverless `falcon` writes to DynamoDB
`falcon-state`, not SQLite, and (as of v0.1.0) **no Lambda actually persists a
screen run anywhere** — the pipeline is pure Step Functions passthrough, and
`grade_tickers` is still a stub. So tradekit's reader pointed at a database
nothing produced.

**Resolution:** falcon now ships
[`shared/sqlite_store.py`](https://github.com/davdunc/falcon), a stdlib-only
exporter that materializes the exact schema above from a pipeline result
(`$.graded.ranked`). `executed_at` is stored as **naive-local** ISO 8601 to
match tradekit's lookback comparison
(`(datetime.now() - timedelta(days=N)).isoformat()`). A contract test
(`tests/test_sqlite_store.py`) re-runs tradekit's own `FalconReader` queries
against the export to guard the contract.

Run it locally to feed tradekit:

```bash
# from a falcon checkout, given a pipeline result JSON
python -m shared.sqlite_store result.json --strategy morning
# → writes falcon's SQLite DB ; then:
tradekit weekly-review --from-falcon --falcon-strategy morning
```

---

## Functional overlap (not a contract — a duplication to be aware of)

falcon and tradekit independently implement much of the same morning job. This
is divergence, not integration:

| Falcon Lambda | tradekit equivalent | Note |
|---------------|---------------------|------|
| `scan_gappers` | premarket scanner / `morning` | both Finviz-based |
| `grade_tickers` | scoring 0–100 (`analysis/scoring.py`) | **falcon's is a stub**; tradekit has the real implementation |
| `calculate_pivots` | `levels` / support-resistance | |
| `build_game_plan` | SMB-style HTML game plan (`cli.py`) | |
| `post_notion` / `post_slack` | alerts + Notion push | |

If the suite consolidates, tradekit's scoring is the natural source for
falcon's missing `grade_tickers` logic.

---

## Version compatibility matrix

Semantic versioning per repo. A row is a known-good combination; the contract
columns record which version of each contract that combo implements.

| falcon | tradekit | falcon-stats | C1 (screen-run SQLite) | C2 (Trade_Review) | Notes |
|--------|----------|--------------|------------------------|-------------------|-------|
| v0.1.0 | v0.1.0   | v0.1.0       | C1.v1 ✅ (via `sqlite_store`) | C2.v1 | Baseline tag for all three. falcon `grade_tickers` still a stub; C3 parquet draft. |

### Contract versions

- **C1.v1** — `screen_runs(id, strategy_name, executed_at)` +
  `screen_results(run_id, symbol, rank)`; `executed_at` = naive-local ISO 8601.
- **C2.v1** — `~/Trade_Review/<YYYY>/<MM>/<YYYY-MM-DD>/` day directories;
  tradekit writes `news.json`, falcon-stats reads `Trades.csv` / `Orders.csv`.
- **C2.v2** — same tree relocated to `$XDG_DATA_HOME/trade-review/`
  (`~/.local/share/trade-review/`). tradekit reads either location; falcon-stats
  must opt in before this becomes the only one. See
  [ADR 0001](adr/0001-xdg-base-directories.md).

---

## Versioning policy

- Each repo is tagged independently with `vMAJOR.MINOR.PATCH`.
- **Bump the MINOR** of a producer when it changes a shared contract in a
  backward-compatible way (adds an optional column/field); **bump MAJOR** for a
  breaking change (renames/removes a column the consumer reads).
- When a contract changes, add a new `Cn.vX` entry above and a new matrix row
  pinning the versions that implement it. Update the consumer's contract test
  (e.g. `falcon/tests/test_sqlite_store.py` mirrors tradekit's reader queries).
- Tag only commits on `main`. Keep each repo's `CHANGELOG.md` in step with its
  tags.
