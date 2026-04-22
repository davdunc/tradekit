"""Settings and configuration management for tradekit."""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings

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


def _resolve_env_files() -> list[str]:
    """Build the .env load chain.

    Pydantic-settings loads files in order, with later files overriding earlier
    ones. Precedence (lowest → highest):

        1. ./.env                 — project-local standalone
        2. ~/.claude/.env         — Claude Code default
        3. $PAI_DIR/.env          — PAI ecosystem shared config
    """
    candidates: list[Path] = [
        PROJECT_ROOT / ".env",
        Path.home() / ".claude" / ".env",
    ]
    pai_dir = os.environ.get("PAI_DIR")
    if pai_dir:
        candidates.append(Path(pai_dir).expanduser() / ".env")
    seen: set[str] = set()
    ordered: list[str] = []
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def shared_env_path() -> Path:
    """Return the recommended shared .env path for write operations.

    Used by `tradekit init` to decide where to persist keys. Prefers
    $PAI_DIR/.env when PAI_DIR is set, otherwise ~/.claude/.env if the
    directory exists, otherwise a project-local .env.
    """
    pai_dir = os.environ.get("PAI_DIR")
    if pai_dir:
        return Path(pai_dir).expanduser() / ".env"
    claude_dir = Path.home() / ".claude"
    if claude_dir.exists():
        return claude_dir / ".env"
    return PROJECT_ROOT / ".env"


_ENV_FILES = _resolve_env_files()


class DataSettings(BaseSettings):
    model_config = {"env_file": _ENV_FILES, "env_file_encoding": "utf-8", "extra": "ignore"}

    alphavantage_api_key: str = ""
    massive_api_key: str = ""
    finviz_api_key: str = ""
    yahoo_cache_ttl_minutes: int = 5
    finviz_cache_ttl_minutes: int = 10
    cache_dir: Path = Path.home() / ".tradekit" / "cache"
    backtest_access_key: str = ""
    backtest_secret_key: str = ""
    backtest_bucket: str = "flatfiles"
    backtest_endpoint: str = "https://files.massive.com"


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
    massive_mcp_package: str = "git+https://github.com/massive-com/mcp_massive@v0.7.0"

    def load_watchlists(self) -> dict[str, list[str]]:
        path = self.config_dir / "watchlists.yaml"
        if not path.exists():
            return {"default": []}
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        # Normalize: ensure all values are lists
        return {k: (v if isinstance(v, list) else []) for k, v in data.items()}

    def load_screener_presets(self) -> dict:
        path = self.config_dir / "screener.yaml"
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def load_indicator_presets(self) -> dict:
        path = self.config_dir / "indicators.yaml"
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}


def get_settings() -> Settings:
    return Settings()
