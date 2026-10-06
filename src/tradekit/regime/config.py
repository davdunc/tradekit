"""Load and validate the regime config. A missing file or key is an error, never a default."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REQUIRED = {
    "version": None,
    "status": None,
    "benchmarks": ("direction", "confirm"),
    "history_sessions": None,
    "direction": ("sma_window", "slope_lag", "atr_window", "up", "down"),
    "structure": ("er_window", "trend_min", "range_max"),
    "volatility": ("hv_window", "lookback", "low_pct", "high_pct", "extreme_pct"),
    "participation": ("universe", "min_universe", "broad", "narrow", "sectors"),
    "transitions": ("confirm_sessions",),
    "data_quality": ("max_bar_age_days",),
}


class RegimeConfigError(ValueError):
    """The regime config is missing or incomplete; classification refuses to guess."""


def default_path() -> Path:
    from tradekit.paths import config_dir

    return Path(os.environ.get("TRADEKIT_REGIME_CONFIG") or config_dir() / "regime.yaml")


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else default_path()
    if not p.exists():
        raise RegimeConfigError(
            f"No regime config at {p}. Review config/regime.example.yaml (proposed, experimental values) and "
            "copy it there, or pass --config. tradekit will not classify on invented thresholds."
        )
    cfg = yaml.safe_load(p.read_text()) or {}
    missing = []
    for key, subkeys in REQUIRED.items():
        if key not in cfg:
            missing.append(key)
        elif subkeys:
            missing += [f"{key}.{s}" for s in subkeys if s not in (cfg[key] or {})]
    if missing:
        raise RegimeConfigError(f"Regime config {p} is missing required keys: {', '.join(missing)}")
    if cfg["status"] not in ("experimental", "validated"):
        raise RegimeConfigError(f"Regime config status must be 'experimental' or 'validated', got {cfg['status']!r}")
    cfg["_path"] = str(p)
    return cfg
