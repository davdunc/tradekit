"""tradekit — Personal trading infrastructure for pre-market screening and technical analysis."""

# Make vendored deps importable as top-level packages (e.g. `import finvizfinance`,
# `import ta`). Vendored at src/tradekit/_vendor/ — pinned versions are listed in
# src/tradekit/_vendor/<pkg>/VENDORED_VERSION and surfaced as Provides: bundled(...)
# in the Fedora .spec.
import os as _os
import sys as _sys

_VENDOR = _os.path.join(_os.path.dirname(__file__), "_vendor")
if _os.path.isdir(_VENDOR) and _VENDOR not in _sys.path:
    _sys.path.insert(0, _VENDOR)

del _os, _sys, _VENDOR

__version__ = "0.1.0"
