"""Append HTTP 403s from paid data APIs to the shared api-errors log.

Same JSON-lines schema as LifeOS ``~/.claude/Tools/api_errors.py``, whose ``report`` command
groups the events and files them as ``bug`` + ``api-403`` issues in the app's repo (tradekit →
davdunc/tradekit). Kept as a copy rather than an import because tradekit installs standalone.

The URL is stored **path-only** with ticker/date segments normalized, because the issues land in
public repos: no query string, key, account or market data is ever written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import urllib.parse
from pathlib import Path

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER_PARENTS = {"ticker", "tickers"}


def log_path() -> Path:
    env = os.environ.get("API_ERROR_LOG")
    if env:
        return Path(env)
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "api-errors" / "events.jsonl"


def normalize(url: str) -> tuple[str, str]:
    """(host, path) with the query dropped and ticker/date path segments replaced by placeholders."""
    parts = urllib.parse.urlsplit(url)
    segs = parts.path.split("/")
    out = []
    for i, s in enumerate(segs):
        if _DATE.match(s):
            out.append("{date}")
        elif i > 0 and segs[i - 1].lower() in _TICKER_PARENTS and s:
            out.append("{ticker}")
        else:
            out.append(s)
    return parts.hostname or "", "/".join(out) or "/"


def _node() -> str:
    try:
        return Path("/etc/hostname").read_text().strip() or socket.gethostname()
    except OSError:
        return socket.gethostname()


def record(provider: str, url: str, status: int, detail: str = "", app: str = "tradekit", method: str = "GET") -> None:
    """Append one redacted event. Never raises — logging must not break the request path."""
    try:
        host, path = normalize(url)
        ev = {
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "node": _node(),
            "app": app,
            "provider": provider,
            "status": int(status),
            "method": method,
            "host": host,
            "path": path,
            "detail": str(detail)[:200],
        }
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")
    except Exception:  # noqa: BLE001 - best effort by design
        pass
