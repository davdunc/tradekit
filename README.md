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

---

## Installation

### Standalone

Requires Python 3.14+ and [`uv`][uv].

```bash
# From a cloned working copy:
uv pip install -e .

# Or directly from GitHub:
uv tool install git+https://github.com/davdunc/tradekit
```

### Inside a LifeOS install

If you're running [LifeOS][pai] (formerly PAI), install tradekit and
let it auto-detect the shared environment:

```bash
uv tool install git+https://github.com/davdunc/tradekit
tradekit init
```

`tradekit init` will detect your LifeOS directory (`$PAI_DIR` or `~/.claude/.env`),
prompt for any missing API keys, and write them to the shared `.env` so every
LifeOS pack picks them up.

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

---

## License

MIT — see [LICENSE](LICENSE).
