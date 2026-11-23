"""Settings and configuration management for tradekit."""

import logging
import os
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from tradekit.paths import cache_dir as _default_cache_dir

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Get current time in US Eastern."""
    return datetime.now(ET)


def _project_root() -> Path:
    """Walk up from this file to find the project root (where pyproject.toml lives)."""
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


PROJECT_ROOT = _project_root()


#: LifeOS (formerly PAI, Personal AI Infrastructure) renamed PAI_DIR to LIFEOS_DIR (#5). The two
#: don't mean the same directory: PAI_DIR was the Claude home holding the shared .env, while
#: LIFEOS_DIR is LifeOS's data and tools directory (default ~/.claude/LIFEOS). LifeOS keeps its
#: shared .env at ~/.claude/.env, which this chain always reads, so PAI_DIR is honoured only as a
#: deprecated fallback for installs that still set it.
LEGACY_PAI_DIR = "PAI_DIR"
LIFEOS_DIR = "LIFEOS_DIR"
_warned_legacy: set[str] = set()


def legacy_pai_dir() -> Path | None:
    """``$PAI_DIR`` expanded, warning once that it is deprecated; None when unset."""
    raw = os.environ.get(LEGACY_PAI_DIR)
    if not raw:
        return None
    if LEGACY_PAI_DIR not in _warned_legacy:
        _warned_legacy.add(LEGACY_PAI_DIR)
        message = (
            f"{LEGACY_PAI_DIR} is deprecated: PAI is now LifeOS, which keeps the shared .env at "
            f"~/.claude/.env. Move any keys there and unset {LEGACY_PAI_DIR}."
        )
        warnings.warn(message, DeprecationWarning, stacklevel=2)
        logging.getLogger(__name__).warning(message)
    return Path(os.path.expandvars(raw)).expanduser()


def lifeos_dir() -> Path:
    """LifeOS's data and tools directory: ``$LIFEOS_DIR``, else ``~/.claude/LIFEOS`` (its default)."""
    raw = os.environ.get(LIFEOS_DIR)
    return Path(os.path.expandvars(raw)).expanduser() if raw else Path.home() / ".claude" / "LIFEOS"


def env_file_candidates() -> list[Path]:
    """Build the .env load chain, in precedence order (lowest → highest).

        1. ./.env                 — project-local standalone
        2. ~/.claude/.env         — Claude Code default, and LifeOS's shared .env
        3. $PAI_DIR/.env          — deprecated (PAI is now LifeOS); read only if PAI_DIR is set

    Public because it is the only sanctioned way to locate credentials on disk.
    Anything that needs a key should search this chain rather than reaching into
    a sibling repository's checkout, which is not portable across machines.
    """
    candidates: list[Path] = [
        PROJECT_ROOT / ".env",
        Path.home() / ".claude" / ".env",
    ]
    pai_dir = legacy_pai_dir()
    if pai_dir:
        candidates.append(pai_dir / ".env")
    seen: set[str] = set()
    ordered: list[Path] = []
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(p)
    return ordered


def _resolve_env_files() -> list[str]:
    """String form of ``env_file_candidates`` for pydantic-settings."""
    return [str(p) for p in env_file_candidates()]


def shared_env_path() -> Path:
    """Return the recommended shared .env path for write operations.

    Used by `tradekit init` to decide where to persist keys: ~/.claude/.env (LifeOS's shared
    .env) if that directory exists, otherwise a project-local .env. A deprecated PAI_DIR still
    wins while it is set, because $PAI_DIR/.env is read last and would shadow a key written
    anywhere else.
    """
    pai_dir = legacy_pai_dir()
    if pai_dir:
        return pai_dir / ".env"
    claude_dir = Path.home() / ".claude"
    if claude_dir.exists():
        return claude_dir / ".env"
    return PROJECT_ROOT / ".env"


_ENV_FILES = _resolve_env_files()

logger = logging.getLogger(__name__)

#: Old environment variable names still read, per ``DataSettings`` field, oldest last (#58).
#: The field's own name (upper-cased, e.g. ``MASSIVE_API_KEY``) always wins over these; an alias
#: only fills a field that is otherwise empty, and warns once that it is deprecated. This table
#: is the only place these names appear: no other module reads a vendor variable directly.
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "massive_api_key": ("POLYGON_API_KEY",),  # Polygon is Massive's old name
    "backtest_access_key": ("MASSIVE_S3_ACCESS_KEY",),
    "backtest_secret_key": ("MASSIVE_S3_SECRET_KEY",),
}

_warned_aliases: set[str] = set()


def env_value(name: str) -> str:
    """``name`` from the process environment, else from the .env chain, with pydantic's precedence.

    The process environment wins; among files, a later one in ``env_file_candidates()`` wins over
    an earlier one, as pydantic-settings resolves ``env_file`` lists. Used only for ``ENV_ALIASES``:
    pydantic loads the canonical names itself, but ignores names that aren't fields, and loading a
    .env into settings doesn't populate ``os.environ`` (the silent blank blotter of 2026-08).
    """
    if os.environ.get(name):
        return os.environ[name]
    for path in reversed(env_file_candidates()):
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        except OSError:
            continue
        for line in reversed(lines):  # the last assignment in a file wins, as in a shell
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() == name:
                value = value.strip().strip("'\"")
                if value:
                    return value
    return ""


class DataSettings(BaseSettings):
    model_config = {"env_file": _ENV_FILES, "env_file_encoding": "utf-8", "extra": "ignore"}

    alphavantage_api_key: str = ""
    # Massive (formerly Polygon): one key for REST and the MCP server; POLYGON_API_KEY is a
    # deprecated alias (ENV_ALIASES). The base URL is a public endpoint, so it may carry a default.
    massive_api_key: str = ""
    massive_api_base: str = "https://api.massive.com"
    finviz_api_key: str = ""
    yahoo_cache_ttl_minutes: int = 5
    finviz_cache_ttl_minutes: int = 10
    cache_dir: Path = Field(default_factory=_default_cache_dir)
    # Massive S3 flat files. MASSIVE_S3_ACCESS_KEY / MASSIVE_S3_SECRET_KEY are aliases (ENV_ALIASES).
    backtest_access_key: str = ""
    backtest_secret_key: str = ""
    backtest_bucket: str = "flatfiles"
    backtest_endpoint: str = "https://files.massive.com"
    # Depth recorder sources (#9). IB Gateway: 4002 is the paper port, 4001 live; TWS 7497 / 7496.
    # Host and port take no default (SPEC "no fallback default", G12): a guessed port is refused,
    # and a refusal reads as "IBKR is down" while the Gateway runs fine on another port.
    ibkr_host: str = ""
    ibkr_port: int | None = None
    ibkr_client_id: int = 17  # a wrong id fails distinctly ("client id in use"), so a default is safe
    # Concurrent market-depth requests the account accepts (SPEC G13); Quote Booster packs raise it.
    # Measured, not taken from TWS's text: on 2026-11-19 the paper account accepted 4 and refused
    # the 5th onward with 309 "Max number (3) of market depth requests". Only drives a warning.
    ibkr_depth_limit: int = Field(default=4, ge=1)
    # Alpaca, under Alpaca's own variable names (APCA_API_KEY_ID / APCA_API_SECRET_KEY).
    # Feed: "iex" (free plan, IEX only) or "sip" (Algo Trader Plus, all US exchanges).
    apca_api_key_id: str = ""
    apca_api_secret_key: str = ""
    alpaca_feed: str = "iex"

    @field_validator("ibkr_port", mode="before")
    @classmethod
    def _blank_port_is_unset(cls, value):
        """``IBKR_PORT=`` (as in .env.example) means unset, not a parse error for every command."""
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _fill_from_deprecated_aliases(self):
        """Fill an empty field from its old variable name (ENV_ALIASES), warning once per name."""
        for field, aliases in ENV_ALIASES.items():
            if getattr(self, field):
                continue
            for alias in aliases:
                value = env_value(alias)
                if not value:
                    continue
                setattr(self, field, value)
                if alias not in _warned_aliases:
                    _warned_aliases.add(alias)
                    message = f"{alias} is deprecated: rename it to {env_var(field)} in .env"
                    warnings.warn(message, DeprecationWarning, stacklevel=2)
                    logger.warning(message)
                break
        return self


def env_var(field: str) -> str:
    """The environment variable a ``DataSettings`` field is read from (pydantic's default: upper case).

    For code that has to *name* a variable (the ``init`` wizard writing .env, the MCP subprocess's
    environment, an error message telling the user what to set), so the name is never retyped.
    """
    if field not in DataSettings.model_fields:
        raise KeyError(f"DataSettings has no field {field!r}")
    return field.upper()


class ScreenerSettings(BaseSettings):
    min_gap_pct: float = 2.0
    min_premarket_volume: int = 200_000
    min_price: float = 2.0
    max_price: float = 200.0
    min_avg_volume: int = 500_000
    max_results: int = 20


class AlertSettings(BaseSettings):
    slack_webhook_url: str = ""
    smtp_host: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    alert_score_threshold: int = 75


class Settings(BaseSettings):
    model_config = {
        "env_file": _ENV_FILES,
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    data: DataSettings = Field(default_factory=DataSettings)
    screener: ScreenerSettings = Field(default_factory=ScreenerSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    config_dir: Path = PROJECT_ROOT / "config"
    data_source: str = "yahoo"

    def load_watchlists(self) -> dict[str, list[str]]:
        path = self.config_dir / "watchlists.yaml"
        if not path.exists():
            return {"default": []}
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Normalize: ensure all values are lists
        return {k: (v if isinstance(v, list) else []) for k, v in data.items()}

    def load_screener_presets(self) -> dict:
        path = self.config_dir / "screener.yaml"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def load_indicator_presets(self) -> dict:
        path = self.config_dir / "indicators.yaml"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}


def get_settings() -> Settings:
    return Settings()
