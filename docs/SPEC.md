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
| **How to set up a dev environment** | [`../CONTRIBUTING.md`](../CONTRIBUTING.md) |

### Non-goals

- Restating Python or PEP conventions. Assume competence.
- Documenting individual functions. Docstrings own that.
- Being aspirational. Every rule below is either enforced today or listed in
  [Known gaps](#known-gaps) as unenforced. A rule with no enforcer is a wish.

## Module structure

Six packages, 6,238 lines. `cli.py` is 2,351 of them — see [Known gaps](#known-gaps).

| Package | Responsibility |
|---|---|
| `paths` | Every on-disk location tradekit reads or writes. The only module that may touch the home directory. |
| `config` | Settings model and `.env` resolution. Pydantic `BaseSettings`, layered. |
| `data` | Market data acquisition. One module per provider, plus the cache. |
| `analysis` | Pure computation over DataFrames — indicators, levels, patterns, scoring, setups, volume. |
| `screener` | Candidate selection: pre-market scan, filters, ranking. |
| `reports` | Rendering only — terminal, markdown, HTML, alerts, blotter. |
| `agents` | LLM-backed workflows: bull/bear debate, inference. |
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
port = int(env["DAS_PORT"])                    # correct — absent config fails loudly
port = int(env.get("DAS_PORT", "9910"))        # wrong
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
| `src/tradekit/__init__.py` `__version__` | Derive via `importlib.metadata.version("tradekit")`, or delete it |
| `packaging/tradekit.spec` `Version:` | Must equal the tag `Source0` points at |
| Git tag `vX.Y.Z` | Must equal `project.version` |

### Release checklist

1. `CHANGELOG.md` — move `[Unreleased]` into a dated version section.
2. Bump `project.version` in `pyproject.toml`.
3. Bump `Version:` in `packaging/tradekit.spec`, reset `Release:` to `1%{?dist}`,
   add a `%changelog` entry.
4. Reconcile the spec's `Requires:` against `pyproject.toml` dependencies, and its
   `Commands:` list against `tradekit --help`.
5. Run the CI gates locally (see below).
6. Tag `vX.Y.Z` and push. `release.yml` verifies the tag and publishes to PyPI.
7. COPR build from the tagged tarball.

Semantic versioning. A new command or provider is a minor bump; a changed output
schema or moved contract path is a major one.

## Testing and CI

`tests/` mirrors the package. Currently 528 lines across `test_analysis`,
`test_config`, `test_filters`, `test_paths`.

**What must have a test:** every analysis function (pure, so there is no excuse),
every path resolver including its legacy fallback, and every screener filter
boundary. Providers may be integration-tested or skipped, but must never require
live credentials to *collect*.

CI gates on Python 3.14:

| Gate | Blocking? |
|---|---|
| `ruff check .` | **Yes** |
| `ruff format --check .` | **Yes** |
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
| G2 | **Version stated in four places, all disagreeing.** `pyproject.toml` 0.2.0, `__init__.py` 0.1.0, `tradekit.spec` 0.3.0, `CHANGELOG.md` top heading 0.2.0. Nothing reads `__version__`. | — | Derive `__version__` from `importlib.metadata`; add a CI check across all four |
| G3 | **Import direction is documented, not enforced.** Nothing fails if `analysis` imports `data` tomorrow. | — | Add an import-direction test |
| G4 | **RPM `Requires:` and `Commands:` drift silently** from `pyproject.toml` and `--help`. | matplotlib was missing; four commands were unlisted | Add a CI check comparing spec metadata to the tree |
| G5 | **`cli.py` is 2,351 lines**, 38% of the codebase, and holds logic that belongs in `screener` and `reports`. | — | Extract incrementally; no new business logic in `cli.py` |
| G6 | **mypy: 156 errors across 57 files.** | `mypy src/tradekit` | Ratchet, do not flip |
| G7 | **`reports/html.py:323` imports a private helper** from `data.finviz` instead of the public `paths.trade_review_day_dir`. | `grep -n _trade_review_day_dir src/tradekit/reports/html.py` | Call the public function |

G1 and G4 are the same failure in two places: a claim published without a check
that the claim is true. G3 and G2 are that failure waiting to happen.
