# Contributing to tradekit

Thanks for considering a contribution! `tradekit` is built to integrate with
[Daniel Miessler's Personal_AI_Infrastructure (PAI)][pai], and contributions
that strengthen that integration — as well as general improvements to
screening, analysis, and data sources — are very welcome.

## Development setup

Requires Python 3.14+ and [`uv`][uv].

```bash
git clone https://github.com/davdunc/tradekit
cd tradekit
uv sync --group dev
```

Run the CLI from your working copy:

```bash
uv run tradekit --help
uv run tradekit morning --top-n 5
```

## Quality checks

Before opening a PR, please run locally:

```bash
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy src/tradekit      # type check
uv run pytest                 # tests
```

CI runs the same checks on every PR. `ruff format --check` is enforced;
type checking is advisory (we're still filling in hints).

## Making changes

- **Fork and branch** from `main`.
- **Small, focused PRs** are easier to review than large sweeping ones.
- **Add tests** for new behavior under `tests/`.
- **Keep commits meaningful** — squash noise, leave a clear history.
- **Commit message style**: imperative first line under 72 chars, blank line,
  body explaining the *why*, not the *what*. See recent commits for examples.

## PAI integration

When touching the `.env` loader, `tradekit init` wizard, or other
PAI-facing surfaces, please preserve these invariants:

- **Single shared `.env`** — `tradekit` must never require a second copy of
  keys already configured in `$PAI_DIR/.env`.
- **Graceful standalone fallback** — `tradekit` must work with zero PAI
  context (no `PAI_DIR`, no `~/.claude/.env`).
- **No personal data in defaults** — account sizes, watchlists, and other
  personal configuration stay in user-owned YAML, never in source.

## Reporting issues

Use [GitHub Issues][issues]. For suspected security issues, follow
[SECURITY.md](SECURITY.md) instead of filing a public issue.

## Code of Conduct

Be kind. Assume good faith. Focus feedback on the code, not the contributor.

[pai]: https://github.com/danielmiessler/Personal_AI_Infrastructure
[uv]: https://github.com/astral-sh/uv
[issues]: https://github.com/davdunc/tradekit/issues
