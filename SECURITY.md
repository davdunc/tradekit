# Security Policy

## Reporting a vulnerability

If you discover a security issue in `tradekit`, **please do not open a public
GitHub issue**. Instead, email the maintainer directly at
`davdunc@davidduncan.org` with:

- A description of the issue and its potential impact
- Steps to reproduce (or a proof-of-concept)
- Any suggested mitigation

You should receive an acknowledgment within 72 hours. Please allow reasonable
time for a fix before any public disclosure.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |

This project is pre-1.0; only the latest minor version receives security fixes
until we reach a stable release.

## Never commit secrets

`tradekit` is designed around a **single shared `.env`** (either
`$PAI_DIR/.env` when inside PAI, or a project-local `.env` standalone). The
project's `.gitignore` protects both.

Rules for contributors and users:

1. **NEVER commit `.env` files** — only `.env.example` with placeholders.
2. **NEVER commit API keys, tokens, webhook URLs, or credentials** anywhere in
   source, tests, fixtures, or documentation. Use placeholders.
3. **NEVER paste keys into GitHub issues, PRs, or discussions.**
4. **Rotate immediately** if you suspect a key has been exposed. API providers
   can see your key in any git commit on any public fork, forever — assume
   exposure means compromise.

## Third-party data sources

`tradekit` queries public market data APIs (Yahoo, Finviz, Alpha Vantage,
Massive.com). Respect each provider's rate limits and terms of service:

- **Yahoo Finance** — no official API, subject to change; use responsibly
- **Finviz Elite** — requires paid subscription for the API
- **Alpha Vantage** — free tier has a rate limit
- **Massive.com** — paid API, use your own key

Do not redistribute data you pull through this tool in violation of a
provider's terms.

## Disclaimer

`tradekit` is a tool for analyzing public market data. Nothing in this
repository is financial advice. Trading carries risk of loss. You are
responsible for your own decisions.
