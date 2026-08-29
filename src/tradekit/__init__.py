"""tradekit — Personal trading infrastructure for pre-market screening and technical analysis."""

import os as _os
import sys as _sys
from importlib.metadata import PackageNotFoundError as _PkgNotFound
from importlib.metadata import version as _pkg_version

# Make vendored deps importable as top-level packages (e.g. `import finvizfinance`,
# `import ta`). Vendored at src/tradekit/_vendor/ — pinned versions are listed in
# src/tradekit/_vendor/<pkg>/VENDORED_VERSION and surfaced as Provides: bundled(...)
# in the Fedora .spec. This must run before anything imports those names.
_VENDOR = _os.path.join(_os.path.dirname(__file__), "_vendor")
if _os.path.isdir(_VENDOR) and _VENDOR not in _sys.path:
    _sys.path.insert(0, _VENDOR)

# Single source of truth for the version is pyproject.toml, read here through the
# installed distribution's metadata. Never hand-maintain a second copy.
try:
    __version__ = _pkg_version("tradekit")
except _PkgNotFound:  # source checkout with no install
    __version__ = "0.0.0.dev0"

del _os, _sys, _VENDOR, _pkg_version, _PkgNotFound
