"""LifeOS (formerly PAI) environment handling (#5).

LifeOS renamed PAI_DIR to LIFEOS_DIR, but the two aren't the same directory: PAI_DIR held the
shared .env, LIFEOS_DIR is LifeOS's data and tools directory (default ~/.claude/LIFEOS), and
LifeOS keeps the shared .env at ~/.claude/.env.
"""

import warnings

import pytest

from tradekit import config
from tradekit.agents import inference


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    for name in ("PAI_DIR", "LIFEOS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_warned_legacy", set())
    monkeypatch.setattr(inference, "LEGACY_INFERENCE_TS", tmp_path / ".claude" / "PAI" / "Tools" / "Inference.ts")
    return tmp_path


def deprecations(fn):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fn()
    return result, [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]


def test_without_pai_dir_the_chain_is_project_then_shared(home):
    chain, warned = deprecations(config.env_file_candidates)
    assert chain == [config.PROJECT_ROOT / ".env", home / ".claude" / ".env"]
    assert warned == []


def test_pai_dir_is_still_read_last_and_warns_once(home, monkeypatch):
    monkeypatch.setenv("PAI_DIR", str(home / "old-pai"))
    chain, warned = deprecations(config.env_file_candidates)
    assert chain[-1] == home / "old-pai" / ".env"
    assert len(warned) == 1 and "PAI_DIR is deprecated: PAI is now LifeOS" in warned[0]
    assert deprecations(config.env_file_candidates)[1] == []  # once per process


def test_pai_dir_pointing_at_the_claude_home_is_not_read_twice(home, monkeypatch):
    monkeypatch.setenv("PAI_DIR", "~/.claude")
    chain, _ = deprecations(config.env_file_candidates)
    assert chain == [config.PROJECT_ROOT / ".env", home / ".claude" / ".env"]


def test_lifeos_dir_is_not_an_env_file_location(home, monkeypatch):
    monkeypatch.setenv("LIFEOS_DIR", str(home / "lifeos"))
    assert home / "lifeos" / ".env" not in config.env_file_candidates()


def test_init_writes_to_the_shared_lifeos_env(home):
    (home / ".claude").mkdir()
    assert config.shared_env_path() == home / ".claude" / ".env"


def test_init_still_follows_a_set_pai_dir_so_its_key_isnt_shadowed(home, monkeypatch):
    monkeypatch.setenv("PAI_DIR", str(home / "old-pai"))
    path, warned = deprecations(config.shared_env_path)
    assert path == home / "old-pai" / ".env" and warned


def test_lifeos_dir_defaults_and_overrides(home, monkeypatch):
    assert config.lifeos_dir() == home / ".claude" / "LIFEOS"
    monkeypatch.setenv("LIFEOS_DIR", "~/elsewhere")
    assert config.lifeos_dir() == home / "elsewhere"


def test_inference_prefers_lifeos_then_a_legacy_pai_install(home, monkeypatch):
    current = home / ".claude" / "LIFEOS" / "TOOLS" / "Inference.ts"
    assert inference.inference_tool() == current  # neither exists: report the LifeOS path

    inference.LEGACY_INFERENCE_TS.parent.mkdir(parents=True)
    inference.LEGACY_INFERENCE_TS.write_text("", encoding="utf-8")
    assert inference.inference_tool() == inference.LEGACY_INFERENCE_TS

    current.parent.mkdir(parents=True)
    current.write_text("", encoding="utf-8")
    assert inference.inference_tool() == current


def test_inference_follows_lifeos_dir(home, monkeypatch):
    monkeypatch.setenv("LIFEOS_DIR", str(home / "custom"))
    assert inference.inference_tool() == home / "custom" / "TOOLS" / "Inference.ts"


def test_missing_inference_names_lifeos(home):
    import asyncio

    result = asyncio.run(inference.call("sys", "user"))
    assert not result.success and "LifeOS Inference.ts not found" in result.error
