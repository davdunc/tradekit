# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Gamma exposure snapshot** — `analysis.gex.compute_gex()` aggregates dealer gamma
  per strike from the CBOE public delayed-quotes chain (free, no API key, real open
  interest) and classifies the regime that governs whether breakouts extend or fade.
  Exposed as `tradekit gex [--ticker SPY] [--max-dte N] [--rate R] [--json]`.
  Same-day (0DTE) contracts contribute and are reported as their own slice; time to
  expiry is measured to the 16:00 ET close and floored at ten minutes, so gamma stays
  finite in the closing minutes and expired contracts are dropped rather than floored.

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
[Unreleased]: https://github.com/davdunc/tradekit/compare/0.2.0...HEAD
[0.2.0]: https://github.com/davdunc/tradekit/releases/tag/0.2.0
[0.1.0]: https://github.com/davdunc/tradekit/releases/tag/v0.1.0
