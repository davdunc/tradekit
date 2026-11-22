"""Massive settings are declared once, in config.py, and read nowhere else (#58).

Two halves:

- **Resolution** against real .env files, not just ``os.environ``: pydantic loads canonical names
  (``MASSIVE_API_KEY``) from the env-file chain, and ``ENV_ALIASES`` fills a field from a
  deprecated name (``POLYGON_API_KEY``, ``MASSIVE_S3_*``) with the same precedence. The blotter's
  old private lookup existed because names outside the model were invisible; these tests pin the
  replacement.
- **Enforcement**: no Massive host or vendor variable name appears as a string literal outside
  ``config.py``.
"""

import ast
import pathlib
import re
import warnings

import pytest

from tradekit import config
from tradekit.config import DataSettings, env_var

VARS = [
    "MASSIVE_API_KEY",
    "MASSIVE_API_BASE",
    "POLYGON_API_KEY",
    "BACKTEST_ACCESS_KEY",
    "BACKTEST_SECRET_KEY",
    "MASSIVE_S3_ACCESS_KEY",
    "MASSIVE_S3_SECRET_KEY",
]


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    """An isolated two-file chain (project, then shared), with no vendor variables in the process."""
    project, shared = tmp_path / "project.env", tmp_path / "shared.env"
    for name in VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "env_file_candidates", lambda: [project, shared])
    monkeypatch.setitem(DataSettings.model_config, "env_file", [str(project), str(shared)])
    monkeypatch.setattr(config, "_warned_aliases", set())

    def write(project_lines=(), shared_lines=()):
        project.write_text("\n".join(project_lines) + "\n", encoding="utf-8")
        shared.write_text("\n".join(shared_lines) + "\n", encoding="utf-8")

    return write


def settings():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        data = DataSettings()
    return data, [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]


# ----- resolution -----


def test_canonical_key_from_the_shared_env_file(env_files):
    env_files(shared_lines=["MASSIVE_API_KEY=shared-key"])
    data, warned = settings()
    assert data.massive_api_key == "shared-key" and warned == []


def test_process_environment_beats_the_files(env_files, monkeypatch):
    env_files(shared_lines=["MASSIVE_API_KEY=from-file"])
    monkeypatch.setenv("MASSIVE_API_KEY", "from-env")
    assert settings()[0].massive_api_key == "from-env"


def test_a_later_file_wins_for_canonical_names_and_aliases_alike(env_files):
    env_files(
        project_lines=["MASSIVE_API_KEY=project", "MASSIVE_S3_ACCESS_KEY=project-s3"],
        shared_lines=["MASSIVE_API_KEY=shared", "MASSIVE_S3_ACCESS_KEY=shared-s3"],
    )
    data, _ = settings()
    assert data.massive_api_key == "shared"  # pydantic: the later env_file wins
    assert data.backtest_access_key == "shared-s3"  # ENV_ALIASES follows the same rule


def test_deprecated_polygon_key_fills_massive_and_warns_once(env_files):
    env_files(shared_lines=["POLYGON_API_KEY=old-name"])
    data, warned = settings()
    assert data.massive_api_key == "old-name"
    assert warned == ["POLYGON_API_KEY is deprecated: rename it to MASSIVE_API_KEY in .env"]
    assert settings()[1] == []  # once per process, not on every get_settings()


def test_the_canonical_name_beats_a_deprecated_alias(env_files):
    env_files(project_lines=["MASSIVE_API_KEY=new"], shared_lines=["POLYGON_API_KEY=old"])
    data, warned = settings()
    assert data.massive_api_key == "new" and warned == []


def test_flat_file_keys_from_their_vendor_names(env_files):
    env_files(shared_lines=["MASSIVE_S3_ACCESS_KEY=ak", "export MASSIVE_S3_SECRET_KEY='sk'"])
    data, warned = settings()
    assert (data.backtest_access_key, data.backtest_secret_key) == ("ak", "sk")
    assert len(warned) == 2


def test_flat_file_keys_under_their_own_names_need_no_alias(env_files):
    env_files(shared_lines=["BACKTEST_ACCESS_KEY=ak", "BACKTEST_SECRET_KEY=sk"])
    data, warned = settings()
    assert (data.backtest_access_key, data.backtest_secret_key) == ("ak", "sk") and warned == []


def test_base_url_defaults_to_massive_and_can_be_overridden(env_files, monkeypatch):
    env_files()
    assert settings()[0].massive_api_base == "https://api.massive.com"
    monkeypatch.setenv("MASSIVE_API_BASE", "https://staging.example")
    assert settings()[0].massive_api_base == "https://staging.example"


def test_env_var_names_a_field_and_refuses_unknown_ones():
    assert env_var("massive_api_key") == "MASSIVE_API_KEY"
    assert env_var("backtest_secret_key") == "BACKTEST_SECRET_KEY"
    with pytest.raises(KeyError):
        env_var("polygon_api_key")  # no longer a field: only an alias in ENV_ALIASES


# ----- enforcement -----

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "tradekit"
HOSTS = ("api.massive.com", "api.polygon.io", "files.massive.com")
VENDOR_VAR = re.compile(r"\b(MASSIVE|POLYGON|BACKTEST)_[A-Z0-9_]+\b")


def literals(path: pathlib.Path) -> list[str]:
    """Host names and vendor variable names in string literals, docstrings excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            text = node.value
            hits = [h for h in HOSTS if h in text] + [m.group(0) for m in VENDOR_VAR.finditer(text)]
            if hits:
                found.append(f"line {node.lineno}: {', '.join(hits)}")
    return found


MODULES = sorted(p for p in SRC.rglob("*.py") if "_vendor" not in p.parts and p.name != "config.py")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(SRC)))
def test_no_massive_host_or_variable_name_outside_config(path):
    assert literals(path) == [], f"{path.name}: declare it in config.DataSettings and read settings.data"


def test_the_checker_catches_hosts_names_and_f_strings(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        '"""Docstrings may mention MASSIVE_API_KEY and api.massive.com."""\n'
        'URL = "https://api.massive.com/v3"\n'
        'KEY = os.environ.get("POLYGON_API_KEY")\n'
        'msg = f"set BACKTEST_ACCESS_KEY for {x}"\n',
        encoding="utf-8",
    )
    found = literals(bad)
    assert len(found) == 3
    assert any("api.massive.com" in f for f in found)
    assert any("POLYGON_API_KEY" in f for f in found)
    assert any("BACKTEST_ACCESS_KEY" in f for f in found)
