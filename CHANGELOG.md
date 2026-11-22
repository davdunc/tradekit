# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`tradekit comps TICKER...`: sector and overnight comps premarket check** (the MU lesson,
  2026-08-24). Asia prices US chips six hours early. The command builds a five-signal table
  per ticker:
  - overnight Asia comps (Samsung and SK Hynix for memory, TSMC for semis);
  - the sector ETF's premarket gap;
  - the inverse-ETF tell, sign-flipped;
  - the QQQ-vs-SPY skew.

  With 3 or more signals against `--direction`, the thesis is downgraded to counter-trend
  and needs a reclaim trigger. The comp map and the ticker→sector table are data and can be
  extended. `docs/workflows/morning-gameplan.md` documents the full premarket process in a
  form any operator or LLM harness can run.

- **`tradekit overnight TICKER`: overnight-risk check before carrying a position.** A stop
  bounds an intraday loss, but the next open can land past it. The audit for Mar 9 to May 29,
  2026 put 42% of that period's loss in overnight and weekend holds.
  - 1σ = price × IV × √(sessions ÷ 252).
  - A carry is sized so that a `--gap-sigmas` (default 2) open against the position stays
    inside `--risk`: shares = risk ÷ max(stop distance, k × 1σ).
  - Verdict `CARRY` / `REDUCE` / `NO CARRY` / `SIZE`.
  - Warns when the stop sits inside a 1σ move, and when IV ÷ HV ≥ 1.25 (an event is priced).
  - Shorts: warns when an ex-dividend date falls inside the hold (with what the short pays),
    and when the borrow is hard to borrow, unavailable, or smaller than the position (IBKR
    ticks 456 and 236). Missing data is reported as *not checked*. These are warnings: the
    open drops by about the dividend, and borrow is a recall risk, not a gap size.
  - Price, IV and HV come from IB Gateway with their freshness stated (live or frozen, never
    delayed), or from `--price` / `--iv` / `--hv`. `--json` for machine use.
  - Core in `tradekit.analysis.overnight` (pure); snapshot in `tradekit.data.ibkr`.

- **`tradekit depth record`: order-book depth recorder** (#9). Depth can't be backfilled,
  so it records as it happens, to Hive-partitioned Parquet (`book` / `trades` / `markers`,
  by date, source and symbol) under `$TRADEKIT_DEPTH_DIR`.
  - `--source auto` (default): DAS first, IB Gateway as the automatic fallback, switching
    back with a short overlap when DAS returns. Failover is driven by a source failing or
    disconnecting, not by silence. Gaps with no source are written as markers.
  - `--source das|ibkr|alpaca`: one source only, retried but never substituted.
  - IBKR through IB Gateway (`tradekit-cli[ibkr]`, `ib_async`), connected read-only.
    `IBKR_HOST` and `IBKR_PORT` have no default (SPEC G12): unset, `--source ibkr` is a
    usage error and `auto` warns that it has no fallback, instead of guessing a port whose
    refusal would read as IBKR being down.
  - Symbols past IBKR's concurrent depth cap (`IBKR_DEPTH_LIMIT`, default 4, as measured) are named in a
    warning before the session starts (SPEC G13). They are still requested, so each refusal
    (TWS 309) is recorded as a marker; repeated symbols are requested once.
  - Alpaca (`tradekit-cli[alpaca]`, `alpaca-py`) is **level 1 only** (best bid/ask and
    trades, `depth_levels=1`) and runs only when explicitly selected.
  - The DAS adapter is a placeholder until it is finished on a host running DAS Trader.
- **`tradekit size`: grade-based position sizing (ASET risk-tool rules).**
  - The grade sets the share of the daily stop at risk: A+ 80%, A 30%, B 15%, C 5%, D 0%.
  - shares = floor(risk ÷ |entry − stop|).
  - Exit plans: standard, Spencer Scalp, Rubberband (VWAP thirds), and Fashionably Late, each with stop-out scenarios.
  - Clearance comes from the 6-step checklist.
  - Opt-in `--max-risk` cap and `--atr` stop floor, plus `--json` output.
  - Spec: `docs/intents/position-sizing.md`. Decision: ADR 0003.
- **`tradekit earnings TICKER`: implied earnings move on the event expiry** (#27).
  - The ATM straddle ÷ spot, priced on the first expiry *after* the release: BMO on day D
    may use D's expiry, AMC (or unknown timing) needs a later one. A same-day contract with
    no event in it is never used. The prototype it replaces read AMZN's 10-29 AMC report
    as 1.55% from the 0DTE straddle instead of 7.57%.
  - Reports `event_expiry` and `expiry_quality` (`event` / `diluted` / `unusable`); with
    no qualifying expiry within 30 days the move is UNAVAILABLE, not a wrong number.
  - Date and BMO/AMC timing come from Finviz Elite's earnings date (one request at most)
    unless `--date` and `--timing` are given. `--json` for machine use.
  - Core in `tradekit.analysis.earnings` (#37); option chain from CBOE delayed quotes.
- **`tradekit init` sets up the account mapping** (#18, #40). It shows where the
  LIVE/SIM mapping comes from and, when neither `$TRADEKIT_ACCOUNT_KINDS` nor
  `~/.config/tradekit/accounts.toml` exists, asks for `ID=LIVE,ID=SIM` pairs and writes
  the file (mode 600 on POSIX, never overwriting an existing one).
- **Canonical Reviews directory: `paths.reviews_dir()`** (#20, #39), documented as
  shared suite contract C4. `$TRADEKIT_REVIEWS_DIR` wins, then whichever of
  `~/.local/share/trading-reviews/` or the LifeOS skill's
  `~/.claude/LifeOS/USER/TRADING/Reviews/` exists, else the XDG path.
  `cards publish` gains `--skip-reviews`.
- **README: "Upgrading an existing machine"** (#40) — how to tell the three install
  shapes apart and upgrade each, and why `uv tool install --force` replaces a hand-written
  shim and drops the environment it sets (e.g. `AWS_PROFILE`). Plus an "Account mapping"
  section.

### Changed

- **`tradekit gex` reads its option chain from Massive's options snapshot by default** (#55).
  CBOE's free chain now refuses automated access.
  - `MassiveREST.option_chain()` pages `GET /v3/snapshot/options/{underlying}` (250 contracts a
    page, only up to `--max-dte`), and `chain_from_snapshot()` maps the results (OCC symbol,
    `open_interest`, `implied_volatility`, the underlying's price and `timeframe`) to the chain
    `compute_gex` already reads. The GEX maths is unchanged.
  - Index roots (SPX, NDX, RUT, VIX, XSP, DJX) are requested as `I:` symbols. A `next_url` is
    followed only on Massive's own API hosts, so the key can't go anywhere else.
  - `--source massive|cboe` (default `massive`). The output's new **Data** line names the source
    and Massive's freshness (`delayed` or `real-time`).
  - Needs `MASSIVE_API_KEY` and an options plan that includes chain snapshots. A 403 says the
    plan doesn't, and is never retried. `--source cboe` explains the CBOE block (#55).
  - Some plans return no underlying price or delay indicator in the options snapshot (seen live
    2026-11-22). Spot then comes from Massive's stock snapshot, and the Data line gives the
    options' and spot's as-of times instead of claiming `delayed` or `real-time`. Index
    underlyings without a level are refused, not guessed.
- **Massive settings are declared once, in `config.py`; Polygon names are deprecated** (#58).
  - `DataSettings` owns `massive_api_key`, `massive_api_base` (default
    `https://api.massive.com`) and the flat-file keys.
  - `POLYGON_API_KEY` and `MASSIVE_S3_ACCESS_KEY` / `MASSIVE_S3_SECRET_KEY` are deprecated
    aliases in `config.ENV_ALIASES`. They're resolved through the same `.env` chain and
    precedence as pydantic, fill a field only when its own variable is unset, and warn once.
  - The `polygon_api_key` setting and `blotter.py`'s private `.env` reader are gone. The REST
    client, blotter, backtest and MCP providers, `init` wizard and `gex` messages read
    settings or `config.env_var()`.
  - `tests/test_config_massive.py` checks resolution against real `.env` files, and fails on
    any Massive host or vendor variable name in a string literal outside `config.py` (it
    flags 17 in `main`).

- **`analysis/` does no I/O** (#49). The SPEC calls `analysis` pure computation, but three
  modules fetched market data themselves.
  - **CBOE chain:** `fetch_cboe_chain` moved from `analysis.gex` to the new `data/cboe.py`
    (raising `CBOEError`). `compute_gex(chain, *, ticker, ...)` and
    `implied_move(chain, *, ticker, event_date, timing)` now take the chain instead of
    fetching it. Passing a ticker where the chain belongs raises `TypeError`.
  - **`comps`:** `comps_symbols(ticker, sector)` lists what to fetch, and
    `build_comps_report(ticker, quotes, premarket, ...)` builds the report from fetched
    provider dicts. The new `data/batch.py` `fetch_each` records a failed request as
    `{"error": ...}`. The CLI fetches shared symbols (QQQ, SPY) once per command, not once
    per ticker.
  - **Enforced:** `tests/test_analysis_purity.py` parses every `analysis/` module and fails on
    network libraries, imports of `data`/`cli`/`reports`/`screener`, and provider calls. It
    flags `comps` and `gex` on the previous code.

- **The distribution is now `tradekit-cli`** (#30). `tradekit` on PyPI belongs to an
  unrelated project, so `pip install tradekit` installed the wrong package and every
  PyPI publish failed. Only the distribution name changed: the command is still
  `tradekit` and the import package is still `tradekit`. The RPM is `tradekit-cli` too,
  with `Provides`/`Obsoletes: tradekit` so existing installs upgrade in place.
  uv tools installed under the old name need a one-time reinstall (README, "Upgrading an
  existing machine").
- **Releases ship on GitHub and PyPI** — `release.yml` builds the wheel and sdist for
  each `v*` tag, attaches them to the GitHub release (it can also be run by hand for an
  existing tag), then publishes them to PyPI as `tradekit-cli` through trusted
  publishing. Tags from before the rename are not published.
- **`cards publish` writes the private review by default** (#20, #39). Without
  `--reviews-dir` it used to skip the write silently; it now writes to
  `paths.reviews_dir()`. Pass `--skip-reviews` for the old behaviour. The file is named
  `<date>-review.md` (was `REVIEW-<date>.md`), matching every other artifact there.
- **A dead scanner exits 3, not 0** (#16, #17, #36, #38). `scan`, `second-day`,
  `morning`, `report` and `gameplan` now stop with *UNAVAILABLE, not empty* (exit 3) when
  the public Finviz screener can't be read or when no candidate resolves at the data
  provider, the same as they already did on a Finviz throttle. Schedulers that treated
  exit 0 + "no results" as normal will now see the failure.

### Fixed

- **Premarket gap contract: "no premarket" is `None`, never a 0% gap** (#51).
  `get_premarket()` now returns `has_premarket`, `gap_pct` and `pre_price` from every
  provider. With no premarket price, `gap_pct` and `pre_price` are `None`, so a closed market
  can't read as a flat one (the #48 bug) wherever the gap is used.
  - **Yahoo:** returns `None`, not `0`, and ignores a leftover `preMarketChangePercent` when
    there is no premarket price.
  - **Finviz Elite:** adds the contract keys. It has no premarket data, so `False`/`None`.
  - **Massive:** returns the contract's shape, and its batch skips tickers without a
    premarket, as Yahoo's does. Its response parsing is broken separately (#52).
  - **Consumers:** the second-day screener no longer fills in `0`. The `second-day` table,
    the game-plan notes, and the terminal and markdown scan reports show a missing gap as
    `n/a`, not `+0.0%` or `nan%`. Both reports fall back to the regular price when there is
    no premarket price, instead of printing `$nan`.
  - **Filters:** `gap_filter` excludes missing gaps on purpose. `price_filter` uses the
    regular price when the premarket price is missing, instead of dropping the row.
- **`tradekit comps` read missing data as a neutral 0.00% move** (#48). Yahoo reports "no
  premarket session" as `gap_pct` 0, so before 04:00 ET and on weekends the sector ETF,
  inverse ETF and index skew all read `+0.00% neutral`. With only the two Asia comps live,
  the verdict could never reach COUNTER-TREND, at the hour a morning check is most likely
  to run.
  - **Premarket rows:** with no premarket session (`has_premarket` false), a row is now
    `n/a`, with the reason. The index-skew row is shown as unavailable rather than left out.
  - **Overnight comps:** a comp counts only if its session ended after the last US close
    (weekdays 16:00 ET). An older one, such as Friday's Samsung session read on a Saturday,
    is shown as `stale` and not counted. Quotes now carry Yahoo's `market_time`; a quote
    without one is counted and labelled "session time unknown".
  - **Verdict:** it now states how many signals were unavailable or stale, and says when
    they are enough to hide a COUNTER-TREND call. With no live signal it is `NO READ` (and says
    COUNTER-TREND can't be ruled out), not `ALIGNED`.
- **Fake test 403s were filed as real Finviz throttling** (#24, #35). Tests fed fake
  403 responses through `FinvizSession` without redirecting `API_ERROR_LOG`, so every
  suite run appended five events to `~/.local/state/api-errors/events.jsonl`, and the
  LifeOS reporter filed them on #24. Every occurrence there came from test runs. Tests
  now use a per-test log (and a per-test Reviews directory).
- **Public-screener fallback reported a quiet tape when it was dead** (#16, #36). Finviz
  moved its screener to `/screener/stocks`, which has no HTML results table, so the
  vendored scraper returned `None` and the no-token fallback printed "No results found".
  A page without a results table now raises `FinvizUnavailable` (a real zero-result
  screen still renders the table and stays empty).
- **Scanners hid a dead data provider** (#17, #38). When the screener found candidates
  but none resolved (bad symbols, an expired key, an outage), the scanners returned
  nothing. They now raise `EnrichmentError`, after re-checking up to three candidates so
  that early-morning symbols with no pre-market trades yet still read as empty. They also
  log a sample of the symbols they enrich, and `second-day` no longer suggests lowering
  thresholds when nothing resolved.
- **Accounts rendered `UNMAPPED` with no explanation** (#18, #40). Reports now log one
  warning naming both ways to configure the mapping, and one per account missing from an
  existing mapping.

## [0.5.0] — 2026-11-01

### Added

- **Windows support** — tradekit now runs natively on Windows as well as Linux / WSL,
  and CI tests both (`windows-latest` joins the matrix). For a Windows Claude session
  that should share one install with WSL, `packaging/windows/` ships `tradekit.cmd`
  (cmd / PowerShell) and a `tradekit` sh shim (Git Bash) that run the WSL-installed
  tradekit through a login shell, so keys, regime config, and the report store stay
  single-sourced.

### Fixed

- **UTF-8 everywhere** — every text read/write now passes `encoding="utf-8"`. On Windows
  the locale default (cp1252) crashed writing report cards that contain `—`.
- **Finviz rate limit is cross-process on Windows** — the shared budget and universe-export
  lock used `fcntl` and silently degraded to per-process on Windows; a new
  `tradekit._filelock.exclusive_lock` uses `fcntl` on POSIX and `msvcrt` on Windows.
- **`cards` CLI tests** — the ingest/trend tests now declare `TRADEKIT_ACCOUNT_KINDS`;
  they had been failing on every platform since ingest stopped defaulting unmapped
  accounts to LIVE. Also cleared two pre-existing ruff errors that kept CI red.

## [0.4.1] — 2026-10-20

### Changed

- **Version roll-forward** — 0.4.0 was bumped in-tree but never tagged, released, or
  published; its entries ship in 0.4.1.

### Added

- **Slow Day Checklist operator doc** — `docs/slow-day-checklist.md` records Roberto's
  five-check prioritization protocol for quiet sessions (big picture, sector ETFs,
  catalysts, premarket dollar volume, relative strength/weakness) with the tradekit
  commands that feed each check, and states their limits explicitly: `regime` is a
  completed-session context read, `groups` ranks rotation but supplies no price levels,
  and premarket VWAP / dollar volume is a platform-side input tradekit does not compute.
  C-size risk and the RVOL 1.5+ / ≤0.8 thresholds are carried as Roberto's attributed
  guidance, not validated automated gates. The executable workflow lives in LifeOS
  (`skills/Trading/Workflows/SlowDayChecklist.md`).
- **DW renderer carries the macro synthesis line** — `render_dw_plan` now renders
  `macro.regime_bias` as a blockquote (the same way `render_game_plan` already does), so
  a slow-day plan's five-check summary reaches the Discipline Workshop view instead of
  being silently dropped. Backward-compatible: fires only when the macro line is set, and
  existing records render unchanged. Regression-tested with a store round-trip of a
  WAIT-plan (no thesis, five-check summary, C-size/RVOL rules) through both renderers —
  nothing padded, nothing dropped. The 3 pre-existing `test_cli_cards` failures and ruff
  errors on main are untouched.

- **Discipline Workshop graduation call (W/L)** — `DisciplineScore.graduation` /
  `DisciplineResult.graduation` compute a binary win/loss independent of the 0-10
  score: any of the four hard-violation criteria (`followed_game_plan`,
  `honored_stops`, `no_revenge_trading`, `account_separation`) being unmet forces
  an `L`, regardless of the numeric total. `graduation_violations()` /
  `graduation_violation_descriptions()` name which criteria failed. Surfaced in
  `render_daily_card`, `render_public_summary`, `render_multi_day_trend` (new
  `W/L` column), `DayRow`/`DAY_COLUMNS`, and the `cards ingest` CLI confirmation
  line, so both machines derive the same graduation call from the same rubric
  flags instead of it being a manual per-day judgment call. An unrecorded hard
  criterion defaults to unmet (violated), matching `discipline_from_flags`'s
  existing omitted-defaults-to-False convention.
- **Gamma exposure snapshot** — `analysis.gex.compute_gex()` aggregates dealer gamma
  per strike from the CBOE public delayed-quotes chain (free, no API key, real open
  interest) and classifies the regime that governs whether breakouts extend or fade.
  Exposed as `tradekit gex [--ticker SPY] [--max-dte N] [--rate R] [--json]`.
  Same-day (0DTE) contracts contribute and are reported as their own slice; time to
  expiry is measured to the 16:00 ET close and floored at ten minutes, so gamma stays
  finite in the closing minutes and expired contracts are dropped rather than floored (#14)

### Changed

- **`tradekit regime` output** (#22). The default is the per-dimension assessment. The old SMA/RSI
  table is `tradekit regime --legacy`; the HTML report's regime data is unchanged.
- **The CLI date banner goes to stderr**, so `--json` stdout is a single parseable document on every
  command.
- **`tradekit scan` and `second-day` use the Finviz Elite universe** when `FINVIZ_AUTH_TOKEN` is set,
  instead of the public screener scraper whose concurrent calls drew 403s (#21). Seed gainers require
  at least 100K average daily volume.

### Fixed

- **Finviz `%` columns were never converted on pandas 3** (#21): text columns are dtype `str`, not
  `object`, so `_coerce_pct` skipped them.
- **`test_rsi_is_in_the_default_quote_columns`** checks behaviour instead of source text (#22).

- **Finviz Elite column codes for `rsi`, `change_open` and `gap`** (#13). Each was one higher
  than the export's actual code, so `rsi` requested *Change from Open*, `change_open` requested
  *Gap*, and `gap` requested *Analyst Recom*. The failure was silent: `get_quote()` reads results
  by header name, the expected header was absent, and callers doing `latest.get("rsi", 0)` turned
  missing data into a literal `0` — a reading that looks maximally oversold. `rsi` is in the
  default `get_quotes()` column set, so this was on the common path. Codes 68–72 were suspected of
  sharing the fault and are confirmed correct.

- **Account kind is resolved from configuration, never guessed** (#11). `DEFAULT_ACCOUNT_KINDS`
  was empty and lookups fell through to `AccountKind.LIVE`, so every account — including a
  simulated book — resolved to LIVE. `default_account_kinds()` now reads
  `$TRADEKIT_ACCOUNT_KINDS` then `[accounts]` in `~/.config/tradekit/accounts.toml`, and an
  unmapped account renders `UNMAPPED (<id>)` rather than claiming a risk basis it has not been
  told. `AccountPnL.kind` is `Optional` so "unknown" is a real state
- **Round-trip blotter renders empty charts for the current session's date.**
  `reports/blotter.py` sourced minute bars only from the Massive S3 flat file, which
  isn't published until the day after the session — a same-day blotter run got
  "no bars in window" on every PNG with no visible error (the failure is logged at
  exception-type granularity only, per CWE-209 discipline, so it read as silent).
  `fetch_minute_bars` now falls back to the Massive/Polygon REST aggs API
  (`POLYGON_API_KEY`, a new `polygon_api_key` setting — separate credential from
  `massive_api_key`/`backtest_*`, see `docs/SPEC.md` gap G9) when the flat file
  isn't available yet. Historical dates are unaffected — S3 remains the primary,
  unchanged path.

## [0.3.0] — 2026-08-29

### Added

- **Canonical reporting layer** — unified schema and grading (#8)
- **Round-trip blotter** — `reports/blotter.py` renders a round-trip blotter with
  Massive S3 flat-file backing (#2, #7). **Note:** the module ships but no
  `@cli.command()` currently registers it — see `docs/SPEC.md` gap G1
- **Development specification** — `docs/SPEC.md`: module boundaries and verified import
  direction, the `DataProvider` contract, path ownership, CLI registration rules, the
  release process, and a register of eight known gaps

### Changed

- **On-disk layout now follows the XDG Base Directory spec** (#4, ADR 0001). Paths resolve
  via `tradekit.paths`; the two Falcon-suite contract paths tolerate both XDG and legacy
  locations so suite components can migrate independently
- **Rebrand** — Personal AI Infrastructure / PAI → LifeOS across docs and metadata
- `__version__` now derives from installed package metadata instead of being a second
  hand-maintained copy

### Fixed

- Blotter handles open positions and resolves Massive S3 credentials correctly (#7)

### Security

- Locked dependencies upgraded to clear 51 known CVEs (#6)

## [0.2.0] — 2026-07-14

### Added

- **Session-anchored VWAP** — `analysis.volume.compute_session_vwap()` computes
  the New York session VWAP (resets at the 09:30 ET open, excludes pre-market),
  distinct from the existing rolling `compute_vwap`
- **15-Minute VWAP Sandwich detector** — `analysis.setups.detect_vwap_sandwich()`
  flags when the 9-EMA and 34-SMA close on opposite sides of session VWAP on 15m
  bars; direction-agnostic (bullish/bearish inferred from which average leads)

## [0.1.0] — 2026-04-22

Initial release. Extracted from the `davdunc/Alvin` monorepo into a
standalone package designed to integrate with Daniel Miessler's
[LifeOS][pai] (formerly Personal AI Infrastructure / PAI).

### Added

- **Pre-market scanner** — top gap movers enriched with volume, float, and
  catalyst data via Finviz + Yahoo
- **Technical analysis** — indicator computation, pattern detection, volume
  profile, composite scoring (0-100, A-F grades)
- **Support/resistance** — multi-timeframe level detection with strength
  ranking and high-volume-node identification
- **Multi-source data providers** — Yahoo Finance (default), Massive.com (tick
  data), S3-compatible backtest flat files
- **News + catalysts** — Finviz Elite integration for market-moving headlines
  with sentiment
- **Watchlists** — YAML-configured named watchlists
- **Reports** — terminal (Rich), markdown, Slack webhook, SMTP email, HTML
  gameplan dashboard
- **`tradekit init` wizard** — mirrors upstream PAI pack install flow; detects
  `$PAI_DIR`, prompts for API keys, upserts to shared `.env`
- **PAI-aware `.env` loading** — precedence order: project-local →
  `~/.claude/.env` → `$PAI_DIR/.env`
- **Shell-style variable expansion** for `$PAI_DIR` (handles
  `${HOME}/.claude` templates)
- **CI workflow** — ruff + mypy + pytest on every PR
- **Release workflow** — tag-triggered PyPI publish via trusted publishing

### Notes

- Requires Python 3.14+
- Methodology references [SMB Capital][smb]'s published playbook material

[pai]: https://github.com/danielmiessler/LifeOS
[smb]: https://www.smbtraining.com/
[Unreleased]: https://github.com/davdunc/tradekit/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/davdunc/tradekit/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/davdunc/tradekit/releases/tag/v0.4.1
[0.3.0]: https://github.com/davdunc/tradekit/compare/0.2.0...24eb38b
[0.2.0]: https://github.com/davdunc/tradekit/releases/tag/0.2.0
[0.1.0]: https://github.com/davdunc/tradekit/releases/tag/v0.1.0
