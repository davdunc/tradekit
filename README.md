# tradekit

[![CI](https://github.com/davdunc/tradekit/actions/workflows/ci.yml/badge.svg)](https://github.com/davdunc/tradekit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![LifeOS-compatible](https://img.shields.io/badge/LifeOS-compatible-purple.svg)](https://github.com/danielmiessler/LifeOS)

> Personal trading infrastructure for pre-market screening, technical analysis, and trade setup evaluation.

`tradekit` is a Python CLI that provides the analytical backbone for an SMB
Capital–style intraday trading workflow: pre-market scans, support/resistance
levels, indicator-based scoring, and news retrieval. It can be used standalone
or as the data engine for a [LifeOS][pai] pack (formerly Personal AI Infrastructure / PAI).

---

## Features

- **Pre-market scanner** — top gap movers with volume, float, and catalyst enrichment
- **Technical analysis** — indicators, patterns, volume profile, scoring (0-100)
- **Support/resistance** — multi-timeframe level detection with strength ranking
- **Multiple data sources** — Yahoo (default), Massive.com, or S3-hosted flat files
- **News + catalysts** — Finviz Elite integration for market-moving stories
- **Watchlists** — YAML-configured named watchlists
- **Reports** — terminal, markdown, and Slack/email alert outputs
- **Canonical report cards** — one grade ladder, R-units, and a fixed discipline
  rubric across game plan and review, persisted as document-oriented records
  (NoSQL-native, object-storage-archivable) so results are comparable day over
  day. See `tradekit cards --help` and `tradekit.reporting`.
- **Discipline Workshop plans** — render the stored game plan in the format the
  MyInvestingClub Discipline Workshop expects in-channel by 9:00 AM market time:
  `tradekit cards gameplan --format dw`.
- **Slow Day Checklist** — the five-check prioritization protocol for quiet sessions
  (big picture → sector ETFs → catalysts → premarket dollar volume → relative
  strength/weakness), with the tradekit commands that feed each check:
  [docs/slow-day-checklist.md](docs/slow-day-checklist.md).

---

## Installation

### Standalone

Requires Python 3.14+ and [`uv`][uv].

```bash
# From PyPI (from the first release after the rename; see below):
uv tool install tradekit-cli

# Or directly from GitHub:
uv tool install git+https://github.com/davdunc/tradekit

# Or from a cloned working copy:
uv pip install -e .
```

The package is **`tradekit-cli`** on PyPI; the command it installs is `tradekit`.
**Don't `pip install tradekit`**: that name belongs to an unrelated project (#30).

### Windows

tradekit runs natively on Windows (`uv tool install git+https://github.com/davdunc/tradekit`).
Native Windows and WSL keep separate home directories, though, so each would have its own
`.env`, regime config, and report store.

To share one WSL install with Windows instead — e.g. Claude for Windows and Claude in WSL
working off the same keys and report history — copy the shims from
[`packaging/windows/`](packaging/windows/) onto your Windows `PATH`
(`%USERPROFILE%\.local\bin` works):

- `tradekit.cmd` — for cmd and PowerShell
- `tradekit` — for Git Bash (what Claude Code's Bash tool uses on Windows)

Both run `tradekit` inside WSL through a login shell. Set `TRADEKIT_WSL_DISTRO` to choose
a distro; otherwise the WSL default is used. The working directory is translated
automatically, but Windows-style path *arguments* are not: pass `--out` relative paths or
`/mnt/c/...` paths. A native `tradekit.exe` in the same directory takes precedence over
`tradekit.cmd`.

### Inside a LifeOS install

If you're running [LifeOS][pai] (formerly PAI), install tradekit and
let it auto-detect the shared environment:

```bash
uv tool install git+https://github.com/davdunc/tradekit
tradekit init
```

`tradekit init` will detect your LifeOS directory (`$PAI_DIR` or `~/.claude/.env`),
prompt for any missing API keys, and write them to the shared `.env` so every
LifeOS pack picks them up. It also offers to write the account mapping
(see [Account mapping](#account-mapping)).

### Upgrading an existing machine

First check **how this machine runs tradekit**. The upgrade command depends on it, and
the wrong one can silently break a working install:

```bash
uv tool list | grep tradekit         # listed → installed with `uv tool install`
head -3 "$(command -v tradekit)"     # a shell script → a hand-written shim
```

| Install shape | How to tell | Upgrade |
|---|---|---|
| **uv tool from PyPI or GitHub** | `uv tool list` shows tradekit-cli | `uv tool upgrade tradekit-cli` |
| **uv tool from a checkout** | `uv tool list` shows tradekit-cli | in your checkout: `git pull && uv tool install --force .` |
| **Editable venv + shim** | `uv tool list` is empty; `tradekit` is a script that `exec`s `<checkout>/.venv/bin/tradekit` | in your checkout: `git pull && uv sync` |

**Once, for installs from before the rename (#30).** If `uv tool list` shows plain
`tradekit`, that tool was installed under the old distribution name, and `uv tool upgrade
tradekit` can't follow the rename. Reinstall it under the new name:

```bash
uv tool uninstall tradekit
uv tool install git+https://github.com/davdunc/tradekit   # or: uv tool install tradekit-cli
```

Editable venvs need nothing extra: `uv sync` swaps the old distribution for the new one,
and a shim pointing at `<checkout>/.venv/bin/tradekit` keeps working.

Two things to watch:

- **Checkout paths differ between machines.** Run the commands from wherever *this*
  machine's checkout lives (`~/src/tradekit`, `~/Projects/Falcon/tradekit`, …) rather than
  pasting a path from another machine. A pasted `cd` that fails leaves the node quietly
  behind.
- **`uv tool install --force` replaces `~/.local/bin/tradekit`.** On a machine that uses a
  shim, that throws the shim away, along with any environment it sets. For example a shim
  like this one exists to set `AWS_PROFILE`:

  ```bash
  #!/usr/bin/env bash
  exec env AWS_PROFILE=falcon-stats ~/Projects/Falcon/tradekit/.venv/bin/tradekit "$@"
  ```

  After a `--force` install, AWS commands silently use the default profile. Keep
  shim machines on `uv sync`. Environment like `AWS_PROFILE` belongs in the shim, not
  in `.env`: tradekit's `.env` files configure tradekit's settings but are not exported
  to libraries such as boto3.

---

## Configuration

Copy `.env.example` to `.env` (or append to your shared LifeOS `.env`) and fill in
the keys you need. Minimum viable setup uses only Yahoo data, which requires
no API key. Add Finviz, Alpha Vantage, or Massive keys to unlock additional
data sources and news.

Environment files are loaded in this precedence order (last wins):

1. System environment variables
2. `$PAI_DIR/.env` — LifeOS ecosystem shared config
3. `~/.claude/.env` — Claude Code default
4. `./.env` — project-local

See [`.env.example`](.env.example) for the full variable list.

### Account mapping

Reports label each broker account LIVE or SIM. The mapping is personal, so it is never
in the repository; it comes from, first hit wins:

1. `$TRADEKIT_ACCOUNT_KINDS`, e.g. `"ACCT1=LIVE,ACCT2=SIM"`
2. `~/.config/tradekit/accounts.toml` (`$XDG_CONFIG_HOME/tradekit/accounts.toml`):

   ```toml
   # chmod 600, local only, never committed
   [accounts]
   "ACCT1" = "LIVE"
   "ACCT2" = "SIM"
   ```

`tradekit init` offers to write that file. With neither configured, every account is
reported as `UNMAPPED` (never guessed as LIVE), and the reporting commands log a warning
naming both options.

---

## Usage

```bash
# Morning pre-market workflow — scan + analyze top N gap movers
tradekit morning --top-n 5

# Analyze a single ticker
tradekit analyze NVDA

# Support/resistance levels
tradekit levels NVDA

# Watchlist scan
tradekit watchlist default

# Today's game plan in Discipline Workshop post format
tradekit cards gameplan --format dw

# A specific day, with session risk limits appended, written to a file
tradekit cards gameplan 2026-08-31 --max-trades 5 --r-dollars 280 --out plan.md

# Interactive setup wizard (populates .env)
tradekit init
```

Prefer `--out` over shell redirection for the plan: `tradekit` prints a session
banner on stdout, so `> plan.md` would capture that too.

Run `tradekit --help` for the full command list.

### Checking overnight risk before a carry

A stop limits an intraday loss, but the next session can open straight past it.
`tradekit overnight` sizes a carry against the move the options market prices, not just
the stop distance:

```sh
tradekit overnight AAPL --stop 335 --risk 100 --shares 20              # live price and IV from IB Gateway
tradekit overnight AAPL --stop 335 --risk 100 --sessions 1 --json      # a weekend counts as one session
tradekit overnight XYZ --stop 9.40 --risk 50 --price 10 --iv 0.65      # no broker: give price and IV
```

- **Expected move:** 1σ = price × IV × √(sessions ÷ 252).
- **Loss per share on a carry:** max(stop distance, `--gap-sigmas` × 1σ). `--gap-sigmas`
  defaults to 2, about 95% of normal outcomes.
- **Carry size:** `--risk` ÷ that loss per share.
- **Verdict:** `CARRY`, `REDUCE` to the carry size, `NO CARRY`, or `SIZE` when no
  `--shares` is given.
- **Warnings:** the stop sits inside a 1σ move, or IV ÷ HV ≥ 1.25, meaning the options are
  pricing an event, so check for earnings or news. Warnings never change the verdict.

Price, 30-day IV and HV come from IB Gateway (`tradekit-cli[ibkr]`, `IBKR_HOST`,
`IBKR_PORT`; client id `IBKR_CLIENT_ID + 1`, so it can run next to `depth record`). The output
says whether the data was live or frozen after the close, and delayed data is refused.

### Recording order-book depth

Order-book depth can't be backfilled, so `tradekit depth record` captures it as it happens
(#9), to Parquet under `$TRADEKIT_DEPTH_DIR` (default `~/.local/share/tradekit/depth/`):

```bash
tradekit depth record AAPL NVDA TSLA --until 16:00          # auto: DAS, falling back to IBKR
tradekit depth record AAPL --source ibkr                     # one source, no fallback
tradekit depth record AAPL --source alpaca                   # level 1 only, explicit
```

| `--source` | Data | Notes |
|---|---|---|
| `auto` (default) | Full depth | DAS first, IB Gateway as the automatic fallback; switches back to DAS with a short overlap when it returns |
| `das` | Full depth, market-maker IDs | Adapter not finished yet (#9): it must be completed on a host running DAS Trader |
| `ibkr` | Full depth | IB Gateway on this machine; install with `tradekit-cli[ibkr]`, connects read-only |
| `alpaca` | **Best bid/ask and trades only** | Never chosen by `auto`; rows carry `depth_levels=1`. Install with `tradekit-cli[alpaca]`, keys in `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` |

Output is partitioned as `book|trades|markers / date= / source= / symbol=`, readable as one
dataset with pandas, pyarrow or DuckDB. Markers record every start, stop, source switch and
gap, so missing depth is never mistaken for a quiet market. Alpaca's feed: `ALPACA_FEED`
(`iex` or `sip`).

IB Gateway needs `IBKR_HOST` and `IBKR_PORT` (Gateway 4002 paper / 4001 live; TWS 7497 / 7496),
plus optional `IBKR_CLIENT_ID` (default 17). Host and port have **no default**: a guessed port
would be refused, and a refusal is recorded as "IBKR unavailable" while the Gateway runs fine on
another port. Unset, `--source ibkr` stops with a usage error and `auto` warns that it has no
fallback this run.

```sh
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
```

IBKR caps concurrent depth requests per account, and Quote Booster packs raise the cap.
Depth is requested in the order given, so symbols past the cap are refused (TWS 309) and
recorded only as error markers. `depth record` warns about them before the session starts
rather than dropping them. `IBKR_DEPTH_LIMIT` defaults to 4, which is what a paper account
measurably accepted. TWS's own refusal text says "Max number (3)", but it accepts a fourth
request. Set `IBKR_DEPTH_LIMIT` to what your account accepts.

---

## Integration with LifeOS

`tradekit` is designed to slot into [Daniel Miessler's LifeOS ecosystem][pai] as
the CLI backbone of a trading skill pack. The companion pack (Trading skill)
provides the conversational workflows — MorningPrep, TradeSetup, SessionReview,
WeeklyReview — and shells out to `tradekit` for market data, scans, and
analysis.

**How LifeOS integration works:**

- `tradekit` reads from the **same shared `.env`** as every other LifeOS pack
  (`$PAI_DIR/.env`), so API keys are configured once
- The **Trading skill** routes natural-language requests (`"morning prep"`,
  `"analyze NVDA"`, `"show levels for SPY"`) to the appropriate `tradekit`
  subcommand
- `tradekit init` mirrors the **wizard-style INSTALL.md** pattern used by
  upstream LifeOS packs — system analysis, key detection, interactive prompts,
  verification

The Trading skill pack is a separate deliverable tracked against upstream LifeOS.

---

## Development

```bash
# Clone and install with dev deps
git clone https://github.com/davdunc/tradekit
cd tradekit
uv sync --group dev

# Run tests
uv run pytest

# Lint + type check
uv run ruff check .
uv run mypy src/tradekit
```

---

## Claude Code GitHub Actions

Two workflows run [Claude Code][claude-action] on this repository:

- **`@claude` on demand** (`.github/workflows/claude.yml`): mention `@claude` in an
  issue, a PR comment, a review comment or a submitted review (or in an issue's title
  or body when it's opened or assigned), and Claude acts on the request: answers a
  question, explains code, or makes a change. It can read CI results, so you can ask
  it about a failing check. Only users with write access to the repository can
  trigger it.
- **Automatic PR review** (`.github/workflows/claude-code-review.yml`): when a pull
  request is opened, reopened, marked ready for review or updated, Claude reviews it
  and posts findings as inline comments.

Both authenticate with the `CLAUDE_CODE_OAUTH_TOKEN` repository secret and the Claude
GitHub App. GitHub doesn't pass secrets to workflows triggered from forks, so on pull
requests from forks the review check fails rather than running; maintainers can ask
for a review with an `@claude` comment instead.

---

## Acknowledgments

This project is built to integrate with [Daniel Miessler's LifeOS][pai] (formerly
Personal AI Infrastructure / PAI) and follows the conventions established there —
shared `.env` configuration, wizard-style installation, and pack-based composition.
Thanks to Daniel and the LifeOS community for the foundation.

Trading methodology draws on [SMB Capital][smb]'s published playbook material
(Bellafiore, Spencer).

[pai]: https://github.com/danielmiessler/LifeOS
[smb]: https://www.smbtraining.com/
[uv]: https://github.com/astral-sh/uv
[claude-action]: https://github.com/anthropics/claude-code-action

---

## License

MIT — see [LICENSE](LICENSE).
