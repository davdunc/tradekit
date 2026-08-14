"""Tests for XDG path resolution (see docs/adr/0001-xdg-base-directories.md)."""

from pathlib import Path

import pytest

from tradekit import paths

XDG_VARS = ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
OVERRIDE_VARS = (
    "TRADEKIT_DATA_DIR",
    "TRADEKIT_CACHE_DIR",
    "TRADEKIT_STATE_DIR",
    "FALCON_DB",
    "TRADE_REVIEW_PATH",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from an environment with no XDG or override vars set."""
    for var in XDG_VARS + OVERRIDE_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_legacy(monkeypatch, tmp_path):
    """Point every legacy location at a path that does not exist."""
    missing = tmp_path / "nonexistent"
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", missing / "market_data")
    monkeypatch.setattr(paths, "LEGACY_CACHE_DIR", missing / ".tradekit" / "cache")
    monkeypatch.setattr(paths, "LEGACY_FALCON_DB", missing / ".falcon" / "falcon.db")
    monkeypatch.setattr(paths, "LEGACY_TRADE_REVIEW_DIRS", (missing / "Trade_Review",))


# --- base directory resolution ----------------------------------------------


def test_xdg_defaults_follow_the_spec(no_legacy):
    home = Path.home()
    assert paths.xdg_data_home() == home / ".local" / "share"
    assert paths.xdg_cache_home() == home / ".cache"
    assert paths.xdg_state_home() == home / ".local" / "state"


def test_xdg_env_var_is_honoured(monkeypatch, tmp_path, no_legacy):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert paths.xdg_data_home() == tmp_path / "data"
    assert paths.data_dir() == tmp_path / "data" / "tradekit"


def test_relative_xdg_var_is_ignored(monkeypatch, no_legacy):
    """The spec says a relative value must be treated as unset."""
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert paths.xdg_data_home() == Path.home() / ".local" / "share"


def test_empty_xdg_var_is_ignored(monkeypatch, no_legacy):
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert paths.xdg_cache_home() == Path.home() / ".cache"


def test_cache_and_data_are_separate_trees(no_legacy):
    """A regenerable cache must not live under the backed-up data dir."""
    assert paths.xdg_cache_home() not in paths.data_dir().parents


# --- the three-step resolution order ----------------------------------------


def test_explicit_override_wins_over_everything(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", legacy)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "tradekit").mkdir(parents=True)
    monkeypatch.setenv("TRADEKIT_DATA_DIR", str(tmp_path / "explicit"))

    assert paths.data_dir() == tmp_path / "explicit"


def test_existing_legacy_dir_wins_over_absent_xdg(monkeypatch, tmp_path):
    """Upgrading must not strand data that is already on disk."""
    legacy = tmp_path / "market_data"
    legacy.mkdir()
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", legacy)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert paths.data_dir() == legacy


def test_xdg_wins_once_it_exists(monkeypatch, tmp_path):
    legacy = tmp_path / "market_data"
    legacy.mkdir()
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", legacy)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / "xdg" / "tradekit"
    expected.mkdir(parents=True)

    assert paths.data_dir() == expected


def test_fresh_install_gets_xdg(monkeypatch, tmp_path, no_legacy):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "xdg" / "tradekit"


# --- shared suite contracts -------------------------------------------------


def test_falcon_db_prefers_legacy_when_only_it_exists(monkeypatch, tmp_path):
    """falcon owns the file; we read it wherever falcon currently writes it."""
    legacy = tmp_path / ".falcon" / "falcon.db"
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    monkeypatch.setattr(paths, "LEGACY_FALCON_DB", legacy)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert paths.falcon_db() == legacy


def test_falcon_db_falls_forward_when_nothing_exists(monkeypatch, tmp_path, no_legacy):
    """With no DB anywhere, name the XDG path so errors point at the new layout."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.falcon_db() == tmp_path / "xdg" / "falcon" / "falcon.db"


def test_falcon_db_env_override(monkeypatch, tmp_path, no_legacy):
    target = tmp_path / "elsewhere" / "falcon.db"
    monkeypatch.setenv("FALCON_DB", str(target))
    assert paths.falcon_db() == target


def test_trade_review_path_env_still_works(monkeypatch, tmp_path, no_legacy):
    """$TRADE_REVIEW_PATH predates this module and is the C2 coordination point."""
    monkeypatch.setenv("TRADE_REVIEW_PATH", str(tmp_path / "shared"))
    assert paths.trade_review_dir() == tmp_path / "shared"


def test_trade_review_day_dir_layout(monkeypatch, tmp_path, no_legacy):
    monkeypatch.setenv("TRADE_REVIEW_PATH", str(tmp_path / "tr"))
    assert paths.trade_review_day_dir(2026, 7, "2026-07-25") == (tmp_path / "tr" / "2026" / "07" / "2026-07-25")


def test_onedrive_legacy_is_still_found(monkeypatch, tmp_path):
    """The old OneDrive default must keep resolving for whoever has data there."""
    onedrive = tmp_path / "OneDrive" / "Documents" / "Trade_Review"
    onedrive.mkdir(parents=True)
    monkeypatch.setattr(paths, "LEGACY_TRADE_REVIEW_DIRS", (tmp_path / "absent", onedrive))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert paths.trade_review_dir() == onedrive


# --- derived paths ----------------------------------------------------------


def test_derived_paths_hang_off_data_dir(monkeypatch, tmp_path, no_legacy):
    monkeypatch.setenv("TRADEKIT_DATA_DIR", str(tmp_path / "d"))
    assert paths.debate_dir() == tmp_path / "d" / "debates"
    assert paths.group_snapshot("2026-07-25") == tmp_path / "d" / "groups_2026-07-25.json"


def test_settings_cache_dir_tracks_paths_module(monkeypatch, tmp_path, no_legacy):
    """config.DataSettings must resolve at instantiation, not at import."""
    from tradekit.config import DataSettings

    monkeypatch.setenv("TRADEKIT_CACHE_DIR", str(tmp_path / "c"))
    assert DataSettings().cache_dir == tmp_path / "c"


def test_falcon_reader_resolves_default_per_instance(monkeypatch, tmp_path, no_legacy):
    from tradekit.data.falcon import FalconReader

    monkeypatch.setenv("FALCON_DB", str(tmp_path / "a.db"))
    assert FalconReader().db_path == tmp_path / "a.db"
    monkeypatch.setenv("FALCON_DB", str(tmp_path / "b.db"))
    assert FalconReader().db_path == tmp_path / "b.db"
