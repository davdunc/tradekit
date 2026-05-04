"""Vendored third-party packages.

This directory contains pure-Python source copies of upstream packages that are
not yet available in Fedora's `python3-*` repos. They are imported as top-level
modules via the sys.path hook in `tradekit/__init__.py`.

| Package | Version | License | Why vendored |
|---------|---------|---------|--------------|
| finvizfinance | 1.3.0 | MIT (Tianning Li) | Not in Fedora repos. Used by `FinvizProvider` (public-site scraper) as the **fallback** when `FINVIZ_AUTH_TOKEN` is unset. The primary code path uses `FinvizEliteProvider` (direct Elite API). |
| ta | 0.11.0 | MIT (Darío López Padial) | Not in Fedora repos. Used by `analysis/indicators.py` for technical indicators (RSI, MACD, Bollinger, etc.). |

Each subdirectory carries its upstream LICENSE plus a `VENDORED_VERSION` file
that the Fedora .spec uses to populate `Provides: bundled(python3-<pkg>) = <ver>`.

When upstream packages land in Fedora, vendoring should be removed and the deps
listed in `pyproject.toml` + `Requires:` lines in the .spec instead.
"""
