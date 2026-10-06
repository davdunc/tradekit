"""Persist assessments; never rewrite one. New information produces a new assessment file."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def regime_dir() -> Path:
    from tradekit.paths import data_dir

    return data_dir() / "regime"


def save(assessment: dict, base: Path | None = None) -> Path:
    base = base or regime_dir()
    d = base / assessment["as_of"]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{assessment['assessment_id']}.json"
    if not p.exists():
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(assessment, indent=2, default=str))
        tmp.replace(p)
    return p


def history(as_of: dt.date, limit: int = 30, base: Path | None = None) -> list[dict]:
    """Latest stored assessment for each session strictly before ``as_of``, oldest first."""
    base = base or regime_dir()
    if not base.exists():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir() and p.name < str(as_of))[-limit:]:
        files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime)
        if files:
            out.append(json.loads(files[-1].read_text()))
    return out
