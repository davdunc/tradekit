# tradekit — Development Specification

**Status:** Living document
**Applies to:** `src/tradekit/` and the packaging around it

This is the spec for working *inside* tradekit: how the package is layered, who owns
which path, what a data provider has to implement, when a feature becomes a command,
and how a release is cut.

## Scope

| This document governs | Where it lives instead |
|---|---|
| Internal module boundaries and import direction | — |
| The `DataProvider` contract | — |
| Configuration precedence and the no-fallback rule | — |
| CLI registration and what "shipped" means | — |
| Version single-source-of-truth and the release checklist | — |
| Vendoring policy | — |
| **Cross-repo contracts with falcon / falcon-stats** | [`falcon-suite-compatibility.md`](falcon-suite-compatibility.md) — C1, C2, C3 and the version matrix |
| **Why paths are XDG-based** | [`adr/0001-xdg-base-directories.md`](adr/0001-xdg-base-directories.md) |
| **Emitted JSON shapes** | [`output-schema.md`](output-schema.md) |
| **Position sizing rules (`tradekit size`)** | [`intents/position-sizing.md`](intents/position-sizing.md) and [`adr/0003-grade-based-position-sizing.md`](adr/0003-grade-based-position-sizing.md) |
| **Market regime rules (`tradekit regime`)** | [`intents/market-regime.md`](intents/market-regime.md) |
| **How to set up a dev environment** | [`../CONTRIBUTING.md`](../CONTRIBUTING.md) |

### Non-goals

- Restating Python or PEP conventions. Assume competence.
- Documenting individual functions. Docstrings own that.
- Being aspirational. Every rule below is either enforced today or listed in
  [Known gaps](#known-gaps) as unenforced. A rule with no enforcer is a wish.

## Module structure

12,261 lines outside `_vendor/` (2026-11-19). `cli.py` is 3,294 of them — see [Known gaps](#known-gaps).
`regime` and `reporting` exist but are not yet described in this table.

| Package | Responsibility |
|---|---|
| `paths` | Every on-disk location tradekit reads or writes. The only module that may touch the home directory. |
| `config` | Settings model and `.env` resolution. Pydantic `BaseSettings`, layered. |
| `data` | Market data acquisition. One module per provider, plus the cache. |
| `analysis` | Pure computation over DataFrames — indicators, levels, patterns, scoring, setups, volume. |
| `screener` | Candidate selection: pre-market scan, filters, ranking. |
| `reports` | Rendering only — terminal, markdown, HTML, alerts, blotter. |
| `agents` | LLM-backed workflows: bull/bear debate, inference. |
| `depth` | Order-book depth recording (#9): normalized events, Parquet sink, the failover recorder, and one adapter per feed under `depth/sources/`. See [Depth recording and source failover](#depth-recording-and-source-failover). |
| `cli` | Click entry points. The only user-facing surface. |

### Import direction

The dependency graph is acyclic and shallow. This is verified, not aspirational:

```
paths      -> (nothing internal)
config     -> paths
data       -> config, paths
analysis   -> config
screener   -> analysis, config, data
reports    -> config, data
agents     -> paths
depth      -> config
cli        -> everything
```

**Rules:**

1. **`paths` imports nothing from tradekit.** It is the floor. If `paths` needs
   configuration, the design is wrong.
2. **`analysis` never imports `data`.** Analysis takes DataFrames in and returns
   values out. This is what makes it testable on synthetic frames with no network,
   no keys and no fixtures — the property that keeps `tests/test_analysis.py`
   fast and hermetic. Breaking it is the single most expensive change you can
   make to this codebase's testability.
3. **`reports` never imports `screener` or `analysis`.** Rendering receives
   finished objects; it does not compute what to render.
4. **Nothing imports `cli`.** If a module needs something in `cli.py`, that
   something is in the wrong file.
5. **No cycles.** Any new edge must preserve the order above.
6. **Import public names across package boundaries.** A `_leading_underscore` symbol belongs
   to its own module. `reports/html.py:323` currently does
   `from tradekit.data.finviz import _trade_review_day_dir`; the public
   `paths.trade_review_day_dir` is what it wants. This is not a layering violation — `reports`
   may import `data` — but it couples a renderer to another package's private helper.

## The data provider contract

`data/base.py` defines a `Protocol`, not a base class — providers are structurally
typed and inherit nothing:

```python
class DataProvider(Protocol):
    def get_quote(self, ticker: str) -> dict: ...
    def get_history(self, ticker: str, period: str = "3mo", interval: str = "1d") -> pd.DataFrame: ...
    def get_premarket(self, ticker: str) -> dict: ...
```

Current implementations: `yahoo` (default), `massive`, `backtest` (S3 flat files),
`finviz` / `finviz_elite`, `falcon` (read-only consumer of contract C1).

**To add a provider:**

1. New module under `data/`. Implement the three methods. Do not subclass anything.
2. `get_history` returns a DataFrame with a `DatetimeIndex` and the OHLCV columns
   the analysis layer expects. Providers normalize; `analysis` must never contain
   a provider-shaped special case.
3. Credentials come from `config`, never from a literal and never from a direct
   `os.environ` read at module scope.
4. Register it for `Settings.data_source` selection.
5. Cache through `data/cache.py`. Do not invent a second cache.

**Freshness is a provider property and must be stated.** A provider that returns
delayed data has to make that visible to callers. A quote whose lag is unknown at
the call site is how a 15-minute-old price ends up on a live order ticket.

**`get_history` cannot serve an explicit historical date — see G9.** It takes only
a relative `period` anchored to `datetime.now()`. A caller that needs bars for one
specific past date (not "the last N days") cannot use the provider layer at all
today; `reports/blotter.py` works around this with a direct fetch instead of going
through `massive`/`backtest`. Know this before reaching for `get_history` to solve
that shape of problem.

## Depth recording and source failover

Order-book depth cannot be backfilled: a session not recorded is lost. `tradekit depth
record` therefore treats **availability** as the first requirement and **honesty about
what was recorded** as the second. Every rule below serves one of those two.

### The source contract

`depth/recorder.py` defines `DepthSource`, a `Protocol` like `DataProvider`:

```python
class DepthSource(Protocol):
    name: str

    async def connect(self) -> None: ...  # raise = unavailable
    async def subscribe(self, symbols: Sequence[str]) -> None: ...
    def events(self) -> AsyncIterator[Event]: ...  # end or raise = disconnected
    async def close(self) -> None: ...
```

- **Raising from `connect` or `subscribe` means "unavailable now".** The recorder retries
  every `--retry` seconds and marks the error only when its text changes.
- **`NotImplementedError` or `PermanentlyUnavailable` means "unavailable for this run".**
  Reported once, then skipped. A missing optional SDK raises `MissingExtra`, which names
  the extra to install (`tradekit-cli[ibkr]`, `tradekit-cli[alpaca]`).
- **`events()` ending or raising is a disconnect**, and triggers failover at once, not at
  the next retry tick.
- Adapters import their broker SDK inside `connect()`, so the base install carries none.

### Source selection

| `--source` | Chain | Rule |
|---|---|---|
| `auto` (default) | `das` → `ibkr` | DAS preferred, IB Gateway the automatic fallback |
| `das` / `ibkr` | that source only | Retried after a failure, **never substituted** |
| `alpaca` | that source only | Level 1. **Never part of `auto`** |

Failover rules, in `Recorder._step`:

1. Fail over when a source fails to connect, raises, or ends its stream. **Never on
   silence**: a thin symbol can print nothing for minutes before the open.
2. When a more preferred source returns, run both for `--overlap` seconds, then stop the
   fallback. A handover has overlap, never a gap.
3. With no source up, write `gap_start`; write `gap_end` when one connects.
4. Every transition is a `Marker` row (`start`, `stop`, `switch`, `error`, `gap_start`,
   `gap_end`) beside the book data, so a reader can tell "empty book" from "not recorded".

### Honesty rules

- **Level 1 never passes for depth.** Every `BookUpdate` carries `depth_levels`; Alpaca
  writes level-1 rows, and the `auto` chain excludes it so the depth dataset is never
  silently downgraded.
- **A refusal is never an empty book.** The IBKR adapter turns every TWS message into an
  `error` marker except the data-farm status codes in `STATUS_CODES` (2104, 2106, 2107,
  2108, 2119, 2158). This is an allowlist of *harmless* codes, not of errors: the first
  live run used an error allowlist, missed 2152 and 10089, and recorded 60 s of nothing
  with no explanation (`dbe85ed`).
- **Read-only, always.** IBKR connects with `readonly=True`; also set the Gateway's
  *Read-Only API*. The DAS adapter must send only login and market-data commands and refuse
  order commands, and the CMD API is reached through an SSH local forward from the DAS
  host, never a port opened to the network: that API can place orders.

### What IBKR depth actually contains

Verified live 2026-11-19 against the paper Gateway (`--source auto`, SPY and AAPL, 45 s):
DAS was skipped, IBKR started as the fallback, and 3,222 book updates were written across
ten levels from AMEX, BYX, CHX, EDGEA, MEMX, NYSENAT, PEARL and others.

- **SMART depth is mixed depth.** On a non-professional account with only the free
  IEX Depth of Book feed, TWS answers each request with 2152
  `Exchanges - Depth: IEX; Top: BYX; AMEX; …`: IEX contributes full depth, the other venues
  only their top of book. The marker records this per symbol; analysis must read it
  rather than assume every venue is full depth.
- **Concurrent depth requests are capped.** The paper account refused a fourth symbol
  with 309 `Max number (3) of market depth requests`. Symbols past the cap are recorded as
  `error` markers, not dropped silently, but they are not recorded.
- **A paper account sees live data only through sharing**, and only while the live
  username is logged out everywhere; otherwise the paper session drops to delayed data.

### Adding a source

1. New module under `depth/sources/`, implementing `DepthSource`. Inherit nothing.
2. Map the feed's messages to `BookUpdate` / `Trade` / `Marker` in pure functions
   (`book_update_from_dom`, `marker_from_tws_error`) so they can be tested without a
   connection.
3. Test those functions against messages **captured live and pasted verbatim**, as
   `tests/test_depth_sources.py` does for IBKR. Hand-written fixtures are how 2152 was
   missed.
4. Register it in `depth/sources/__init__.py:factories()` and `SOURCES`. Add it to
   `AUTO_CHAIN` only if it delivers true depth.
5. Connection settings resolve under the no-fallback rule below.

## Configuration

Resolution order, widest to narrowest — later wins:

1. Package defaults in `config.py`
2. Shared LifeOS `.env`, when present (`$PAI_DIR/.env` or `~/.claude/.env`)
3. Project-local `.env`
4. Process environment
5. Explicit CLI flags

Three invariants, from `CONTRIBUTING.md` and kept here because they constrain code:

- **One shared `.env`.** tradekit never requires a second copy of a key the
  ecosystem already holds.
- **Standalone must work.** Zero LifeOS context — no `PAI_DIR`, no `~/.claude/.env` —
  is a supported configuration, not a degraded one.
- **No personal data in defaults.** Account sizes, watchlists, broker identifiers
  and machine-specific paths live in user-owned YAML or `.env`. This repository is
  public; treat every default as published.

### Connection parameters take no fallback default

**Host, port, and credentials for a user-configured service resolve from
configuration or raise. They do not fall back.**

```python
port = int(env["DAS_PORT"])  # correct — absent config fails loudly
port = int(env.get("DAS_PORT", "9910"))  # wrong
```

The failure mode is specific and expensive: a wrong default produces a connection
refusal, and a connection refusal is **indistinguishable from the service being
down**. The caller reports an outage that is not happening, then routes around a
service that was working the whole time.

This is not hypothetical. On 2026-08-28 a sibling tool in this suite probed a
factory-default port, reported "service down", and a downstream consumer silently
fell back to a 15-minute-delayed data source for a live workflow.

**Scope of the rule.** It binds parameters a user configures for *their own*
environment. A well-known public service endpoint may carry a default —
`backtest_endpoint = "https://files.massive.com"` is fine, because it is the same
for everyone and a wrong value fails at DNS, not ambiguously at connect.

## Paths

`paths.py` is the single source of truth for on-disk layout. **No module outside
`paths.py` constructs a path from the home directory.** Import a function instead.

Two categories, and the difference decides whether you may move a file:

| Category | Meaning | Examples |
|---|---|---|
| **Owned** | tradekit may relocate freely | `data_dir()`, `cache_dir()`, `state_dir()`, `debate_dir()`, `group_snapshot()` |
| **Contract** | Shared with another repo; cannot move unilaterally | `falcon_db()` (C1), `trade_review_dir()` (C2) |

Contract paths resolve both the XDG location and the legacy one, so suite
components can migrate independently. See
[`falcon-suite-compatibility.md`](falcon-suite-compatibility.md) for the contracts
themselves and [ADR 0001](adr/0001-xdg-base-directories.md) for why.

## CLI

`cli.py` is the only user-facing surface. Commands are Click functions registered
with `@cli.command()`.

**The registration rule:** *if any changelog entry, README section, RPM spec, or
release note claims a command exists, `tradekit --help` must list it.*

Note what this does **not** say. Not every module needs an entry point — tradekit is
importable as a library and internal modules are legitimate. The invariant is about
**advertised** surface. A feature that is documented but unreachable is worse than
one that was never shipped, because the documentation asserts otherwise.

The RPM spec's `Commands:` list in `%description` is part of that advertised surface
and must match `--help`.

## Version and release

**`pyproject.toml` is the single source of truth for the version.** `release.yml`
already enforces this — it refuses to publish when the git tag disagrees with
`project.version`.

Everything else derives or matches:

| Location | Rule |
|---|---|
| `pyproject.toml` `project.version` | **Authoritative** |
| `src/tradekit/__init__.py` `__version__` | **Derived** via `importlib.metadata.version("tradekit-cli")` (falling back to the pre-#30 `"tradekit"`). Never hand-maintained |
| `packaging/tradekit-cli.spec` `Version:` | Must equal the tag `Source0` points at |
| Git tag `vX.Y.Z` | Must equal `project.version` |

### Release checklist

1. `CHANGELOG.md` — move `[Unreleased]` into a dated version section.
2. Bump `project.version` in `pyproject.toml`.
3. Bump `Version:` in `packaging/tradekit-cli.spec`, reset `Release:` to `1%{?dist}`,
   add a `%changelog` entry.
4. Reconcile the spec's `Requires:` against `pyproject.toml` dependencies, and its
   `Commands:` list against `tradekit --help`.
5. Run the CI gates locally (see below).
6. Tag `vX.Y.Z` and push. `release.yml` verifies the tag, builds the wheel and sdist,
   attaches them to the GitHub release (creating it from the CHANGELOG section if
   needed), then publishes them to PyPI as **`tradekit-cli`** through trusted
   publishing. The distribution was renamed from `tradekit`, which on PyPI belongs to
   an unrelated project ([#30](https://github.com/davdunc/tradekit/issues/30)); the
   command and import package are still `tradekit`.
7. COPR build from the tagged tarball.

**One-time PyPI setup** (before the first `tradekit-cli` release): on pypi.org, add a
*pending trusted publisher* for project `tradekit-cli`, owner `davdunc`, repository
`tradekit`, workflow `release.yml`, environment `pypi`.

Semantic versioning. A new command or provider is a minor bump; a changed output
schema or moved contract path is a major one.

## Testing and CI

`tests/` mirrors the package. Currently 528 lines across `test_analysis`,
`test_config`, `test_filters`, `test_paths`.

**What must have a test:** every analysis function (pure, so there is no excuse),
every path resolver including its legacy fallback, and every screener filter
boundary. Providers may be integration-tested or skipped, but must never require
live credentials to *collect*. Depth source mappings are tested against verbatim live
messages, and the recorder's failover is tested with fake sources
(`tests/test_depth_*.py`, 33 tests); none needs a broker to collect.

CI gates on Python 3.14:

| Gate | Blocking? |
|---|---|
| `ruff check .` | **Yes** |
| `ruff format --check .` | **Yes** — note this formats Python **inside markdown fences too**, so `docs/*.md` is subject to it. `ruff check` is not; it skips markdown. Run `ruff format --check .` from the repo root before pushing docs |
| `pytest` | **Yes** |
| `mypy src/tradekit` | No — `continue-on-error` |
| Build wheel + sdist | **Yes** |

mypy is advisory because there are currently **156 errors across 57 files**. The
honest near-term target is a ratchet — no *new* errors — not a flip to blocking,
which would stop every PR on day one.

## Vendoring

`finvizfinance` (1.3.0, MIT) and `ta` (0.11.0, MIT) are vendored at
`src/tradekit/_vendor/` and made importable by a `sys.path` hook in
`tradekit/__init__.py`.

- Pinned versions live in `_vendor/<pkg>/VENDORED_VERSION`.
- The RPM spec declares them as `Provides: bundled(...)`.
- Vendored code is excluded from ruff and is never reformatted to this project's
  style — it is an upstream copy, and local edits make the next sync unmergeable.
- **Exit condition:** when Fedora ships `python3-finvizfinance` or `python3-ta`,
  drop the vendored copy and convert to a `Requires:`. Fedora Packaging Guidelines
  forbid bundling except by explicit exception, so this is a hard constraint for
  distribution inclusion even though it is a soft one upstream.

## Security and privacy

This repository is public.

- No API keys, tokens, broker account identifiers, or credentials in source,
  config defaults, test fixtures, or committed `.env` files. `.env` is gitignored;
  keep it that way.
- No personal filesystem paths. Paths come from `paths.py`, which derives from XDG
  environment variables.
- No account sizes, position sizes, or risk parameters in defaults.
- Suspected vulnerabilities follow [`SECURITY.md`](../SECURITY.md), not public issues.

## Known gaps

Recorded rather than hidden. Each is a rule above that nothing currently enforces.

| # | Gap | Evidence | Fix |
|---|---|---|---|
| G1 | **`blotter` is documented but unreachable.** PR #2 advertises `tradekit blotter DATE`; `reports/blotter.py` ships; no `@cli.command()` registers it and `--help` does not list it. | `grep blotter src/tradekit/cli.py` returns nothing | Register the command, or remove the claim |
| ~~G2~~ | ~~**Version stated in four places, all disagreeing.**~~ **Closed 2026-08-29.** `__version__` now derives from installed metadata; `pyproject.toml`, `packaging/tradekit.spec` and `CHANGELOG.md` all read 0.3.0. | `pyproject`/`spec`/`CHANGELOG` all 0.3.0; `tradekit.__version__` derived | Remaining: a CI check asserting the three files still agree — otherwise this reopens (see G4) |
| G3 | **Import direction is documented, not enforced.** Nothing fails if `analysis` imports `data` tomorrow. | — | Add an import-direction test |
| G4 | **RPM `Requires:` drifts silently** from `pyproject.toml`. *`Commands:` half closed 2026-11-10:* `tests/test_packaging.py` fails when the `%description` list and `cli.commands` disagree (it caught `cards` and `gex` missing). | matplotlib was missing | Extend the test to compare `Requires:` with `pyproject.toml` dependencies |
| G5 | **`cli.py` is 3,294 lines** (2026-11-19), 27% of the codebase, and holds logic that belongs in `screener` and `reports`. | — | Extract incrementally; no new business logic in `cli.py` |
| G6 | **mypy: 156 errors across 57 files.** | `mypy src/tradekit` | Ratchet, do not flip |
| G7 | **`reports/html.py:323` imports a private helper** from `data.finviz` instead of the public `paths.trade_review_day_dir`. | `grep -n _trade_review_day_dir src/tradekit/reports/html.py` | Call the public function |
| G8 | ~~**Account kind classification is broken; `main` is red.**~~ **CLOSED by #11 (`5ad22cc`).** `DEFAULT_ACCOUNT_KINDS = {}` fell through to `AccountKind.LIVE` for every account, so a SIM book reported as LIVE, and 3 `tests/test_reporting.py` tests failed on `main`. `default_account_kinds()` now resolves `$TRADEKIT_ACCOUNT_KINDS` then `[accounts]` in `~/.config/tradekit/accounts.toml`; an unmapped account renders `UNMAPPED (<id>)` instead of claiming a risk basis it was never told. | `pytest tests/test_reporting.py` → 41 passed | Done. Kept in the register because the *shape* recurs: a hardcoded value removed for a good reason, its replacement never written, and a permissive default that made the gap invisible rather than loud |
| G9 | **`get_history` cannot serve an explicit historical date, and the same vendor now has four divergent credential schemes.** `reports/blotter.py` needs bars for one specific past (or current) date, not a relative `period` anchored to `now()`, so it cannot use the `DataProvider` layer at all — it fetches Massive S3 flat files directly with `boto3` (`MASSIVE_S3_ACCESS_KEY`/`MASSIVE_S3_SECRET_KEY`), and falls back to the Massive/Polygon REST aggs API (`POLYGON_API_KEY`) when the flat file for the current date hasn't been published yet (confirmed: it 403s/NoSuchKeys until the day after the session). That's on top of the already-separate `massive_api_key` (MCP path, `MassiveProvider`) and `backtest_access_key`/`backtest_secret_key` (`BacktestProvider`'s own S3 fields, not populated by the shared `.env` at all). Four names, one vendor. | `grep -n 'api_key\|access_key\|secret_key' src/tradekit/config.py` → `massive_api_key`, `backtest_access_key`, `backtest_secret_key`, `polygon_api_key` | Extend `get_history` with optional `from_date`/`to_date` so a date-specific caller can use the provider layer; consolidate the credential names (likely onto whatever the Massive dashboard actually issues as one pair) once a provider other than `blotter.py` needs same-day historical bars |
| G10 | **3 tests fail on `main` as of `995ec5a`.** `TestCardsIngest::test_ingest_persists_a_retrievable_card`, `TestCardsIngest::test_ingest_without_narrative_still_persists_falcon_numbers`, `TestCardsTrend::test_trend_lists_both_days_date_sorted` — at least one failure's assertion output embeds today's wall-clock date ("Thu Oct 01... Overnight"), suggesting a date-relative fixture rather than a frozen one. Found incidentally while verifying the same-day blotter fallback (G9) didn't introduce a regression; not investigated further. | `pytest tests/test_cli_cards.py -q` on a clean `main` checkout → 3 failed, 26 passed | Pin the fixture's reference date (e.g. `freezegun` or an explicit `as_of`) so the test doesn't drift with the calendar |
| G11 | **The DAS depth adapter is a placeholder, so `auto` is IBKR-only in practice.** `DASSource.connect()` raises `NotImplementedError`; every `auto` run opens with `error das skipped for this run` and records from the fallback. Finishing it needs a host running DAS Trader. | `src/tradekit/depth/sources/das.py`; live run 2026-11-19 → first marker is the DAS skip | Implement on `daslaptop` per the module docstring: SSH-forwarded CMD API, read-only, Level 2 parsed to `BookUpdate`, socket close ends `events()`, verbatim-capture fixture tests |
| G12 | **`ibkr_host` / `ibkr_port` violate the no-fallback rule.** `config.py` defaults them to `127.0.0.1:4002`. In the recorder this is the exact failure the rule describes: a wrong port is a connection refusal, recorded as "ibkr unavailable" and a `gap_start`, while the Gateway runs fine on another port (4001 live, 7496/7497 TWS). | `grep -n 'ibkr_' src/tradekit/config.py` | Drop the defaults; when `ibkr` is selected or reached in `auto` with no `IBKR_PORT`, raise `PermanentlyUnavailable` naming the variable |
| G13 | **No pre-check against the IBKR depth cap.** Symbols past the account's concurrent-depth limit (3 on the paper account) are refused one by one with 309 at run time. Visible as markers, but the user learns it only after the session started. | Live 2026-11-19: 309 `Max number (3) of market depth requests` | Warn at `depth record` start when `--source` can reach `ibkr` with more symbols than a configured cap; never truncate the list silently |

G1 and G4 are the same failure in two places: a claim published without a check
that the claim is true. G3 and G2 are that failure waiting to happen.
