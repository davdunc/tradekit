"""Fetch regime inputs from Massive: at most three requests (two benchmarks' daily bars + one grouped session)."""

from __future__ import annotations

import datetime as dt

from tradekit.data.massive_rest import MassiveError, MassiveREST


def fetch_inputs(as_of: dt.date, cfg: dict, client: MassiveREST | None = None) -> dict:
    client = client or MassiveREST()
    start = as_of - dt.timedelta(days=int(cfg["history_sessions"] * 1.6) + 10)
    end = as_of - dt.timedelta(days=1)
    out: dict = {"errors": [], "source": "massive"}
    for role in ("direction", "confirm"):
        sym = cfg["benchmarks"][role]
        try:
            out[role] = client.daily_bars(sym, start, end)
        except MassiveError as e:
            out[role] = None
            out["errors"].append(f"{sym}: {e}")
    bench = out.get("direction")
    out["grouped"], out["grouped_date"] = None, None
    if bench is not None and not bench.empty:
        day = bench["date"].iloc[-1]
        try:
            g = client.grouped_daily(day)
            out["grouped"], out["grouped_date"] = (g if not g.empty else None), day
            if g.empty:
                out["errors"].append(f"grouped {day}: empty")
        except MassiveError as e:
            out["errors"].append(f"grouped {day}: {e}")
    out["requests"] = client.requests_made
    return out
