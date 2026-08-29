"""On-disk layout for tradekit, resolved via the XDG Base Directory spec.

Single source of truth for every path tradekit reads or writes. Nothing else in
the package should build a path from ``Path.home()`` — import from here instead,
so the layout can change in one place.

Ownership matters as much as location. tradekit is free to move the files it
owns, but two of these paths are shared contracts with the rest of the Falcon
suite (see ``docs/falcon-suite-compatibility.md``) and cannot be relocated
unilaterally:

- ``falcon_db()`` — contract C1. falcon *writes* it, tradekit only reads.
- ``trade_review_dir()`` — contract C2. tradekit writes ``news.json`` here;
  falcon-stats reads its own files from the same tree.

For those two, resolution tolerates both the XDG location and the legacy one so
the suite keeps working while its components migrate independently.

Spec mapping (https://specifications.freedesktop.org/basedir-spec/latest/):

    $XDG_DATA_HOME   ~/.local/share   durable app data      -> data_dir()
    $XDG_CACHE_HOME  ~/.cache         regenerable, unbacked -> cache_dir()
    $XDG_STATE_HOME  ~/.local/state   logs, history         -> state_dir()
    $XDG_CONFIG_HOME ~/.config        user configuration    -> config_dir()

Note ``~/.local/<app>`` is *not* an XDG location; only ``share``, ``state``,
``bin``, and ``lib`` live directly under ``~/.local``.
"""

from __future__ import annotations

import os
from pathlib import Path

# Pre-XDG locations. Kept only so existing installs are not orphaned; a
# directory here still wins over the XDG path when it is the one that exists.
LEGACY_DATA_DIR = Path.home() / "market_data"
LEGACY_CACHE_DIR = Path.home() / ".tradekit" / "cache"
LEGACY_FALCON_DB = Path.home() / ".falcon" / "falcon.db"
LEGACY_TRADE_REVIEW_DIRS = (
    Path.home() / "Trade_Review",
    # Windows-only artifact of one developer's OneDrive layout; resolves to a
    # nonexistent path under WSL and on Linux.
    Path.home() / "OneDrive" / "Documents" / "Trade_Review",
)


def _env_path(var: str) -> Path | None:
    """Read an absolute path from the environment, or None.

    Per the XDG spec, a variable that is unset, empty, or holding a relative
    path is treated as unset.
    """
    raw = os.environ.get(var)
    if not raw:
        return None
    path = Path(os.path.expandvars(raw)).expanduser()
    return path if path.is_absolute() else None


def xdg_data_home() -> Path:
    return _env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share"


def xdg_cache_home() -> Path:
    return _env_path("XDG_CACHE_HOME") or Path.home() / ".cache"


def xdg_state_home() -> Path:
    return _env_path("XDG_STATE_HOME") or Path.home() / ".local" / "state"


def xdg_config_home() -> Path:
    return _env_path("XDG_CONFIG_HOME") or Path.home() / ".config"


def config_dir() -> Path:
    """User configuration. Owned by tradekit; safe to relocate."""
    return xdg_config_home() / "tradekit"


def accounts_config() -> Path:
    """Account id -> book kind mapping.

    Deliberately user-owned and outside the repository: broker account ids are
    personal identifiers and this project is public.
    """
    return config_dir() / "accounts.toml"


def _resolve(env_var: str, preferred: Path, *legacy: Path) -> Path:
    """Resolve a path: explicit override, then whichever location exists.

    An existing legacy location wins over a non-existent XDG one so that
    upgrading tradekit never silently strands data that is already on disk.
    Once nothing legacy exists — a fresh install, or after the operator moves
    the directory — the XDG path is what gets created.
    """
    override = _env_path(env_var)
    if override is not None:
        return override
    if preferred.exists():
        return preferred
    for candidate in legacy:
        if candidate.exists():
            return candidate
    return preferred


# --- Paths tradekit owns outright -------------------------------------------


def data_dir() -> Path:
    """Durable tradekit output: group snapshots, risk metrics, debates."""
    return _resolve("TRADEKIT_DATA_DIR", xdg_data_home() / "tradekit", LEGACY_DATA_DIR)


def cache_dir() -> Path:
    """Regenerable provider caches. Safe to delete; excluded from backups."""
    return _resolve("TRADEKIT_CACHE_DIR", xdg_cache_home() / "tradekit", LEGACY_CACHE_DIR)


def state_dir() -> Path:
    """Persistent-but-disposable state (debug dumps, scratch HTML)."""
    return _resolve("TRADEKIT_STATE_DIR", xdg_state_home() / "tradekit")


def debate_dir() -> Path:
    """Bull/bear debate transcripts."""
    return data_dir() / "debates"


def group_snapshot(date_iso: str) -> Path:
    """Group-rotation snapshot for a given ``YYYY-MM-DD``."""
    return data_dir() / f"groups_{date_iso}.json"


# --- Shared suite contracts — not ours to relocate alone --------------------


def falcon_db() -> Path:
    """falcon's screening database (contract C1; falcon writes, we read).

    ``$FALCON_DB`` wins, then whichever of the XDG or legacy path exists. When
    neither exists the XDG path is returned so the "no database yet" error
    names the location falcon should be migrating toward.
    """
    return _resolve("FALCON_DB", xdg_data_home() / "falcon" / "falcon.db", LEGACY_FALCON_DB)


def trade_review_dir() -> Path:
    """Root of the Trade_Review tree (contract C2; shared with falcon-stats).

    ``$TRADE_REVIEW_PATH`` is honoured first — it predates this module and is
    the coordination point between suite components that disagree on layout.
    """
    return _resolve(
        "TRADE_REVIEW_PATH",
        xdg_data_home() / "trade-review",
        *LEGACY_TRADE_REVIEW_DIRS,
    )


def trade_review_day_dir(year: int, month: int, date_iso: str) -> Path:
    """Day directory within the Trade_Review tree: ``<root>/YYYY/MM/YYYY-MM-DD/``."""
    return trade_review_dir() / str(year) / f"{month:02d}" / date_iso
