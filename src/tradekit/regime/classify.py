"""Deterministic regime classification: inputs + config + prior state -> one assessment dict.

No I/O and no clock. The caller supplies ``as_of``, the bars, the grouped session and any history,
so the same inputs always give the same output (``assessment_id`` is a hash of them).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pandas as pd

from tradekit.regime import features as F

SCHEMA_VERSION = "1.0"
CLASSIFIER_VERSION = "1.0.0"


def _prior_bars(bars: pd.DataFrame | None, as_of: dt.date) -> pd.DataFrame:
    """Only sessions strictly before as_of: future bars cannot reach an earlier assessment."""
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    return bars[bars["date"] < as_of].sort_values("date").reset_index(drop=True)


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(x, n)


def classify_direction(bars: pd.DataFrame, cfg: dict) -> tuple[str, dict]:
    d = cfg["direction"]
    cvs = F.close_vs_sma(bars, d["sma_window"], d["atr_window"])
    slope = F.sma_slope(bars, d["sma_window"], d["slope_lag"], d["atr_window"])
    ev = {"close_vs_sma_atr": _r(cvs), "sma_slope_atr": _r(slope), "sma_window": d["sma_window"]}
    if cvs is None or slope is None:
        return "unknown", ev | {"reason": "insufficient_bars"}
    if cvs >= d["up"]["close_vs_sma_min"] and slope >= d["up"]["slope_min"]:
        return "up", ev
    if cvs <= d["down"]["close_vs_sma_max"] and slope <= d["down"]["slope_max"]:
        return "down", ev
    return "neutral", ev


def classify_structure(bars: pd.DataFrame, cfg: dict) -> tuple[str, dict]:
    s = cfg["structure"]
    er = F.efficiency_ratio(bars, s["er_window"])
    ev = {"efficiency_ratio": _r(er), "er_window": s["er_window"]}
    if er is None:
        return "unknown", ev | {"reason": "insufficient_bars_or_no_movement"}
    if er >= s["trend_min"]:
        return "trend", ev
    if er <= s["range_max"]:
        return "range", ev
    return "transition", ev


def classify_volatility(bars: pd.DataFrame, cfg: dict) -> tuple[str, dict]:
    v = cfg["volatility"]
    res = F.hv_percentile(bars, v["hv_window"], v["lookback"])
    if res is None:
        return "unknown", {"reason": "insufficient_bars", "lookback": v["lookback"]}
    hv, pct = res
    ev = {"hv": _r(hv, 2), "hv_percentile": _r(pct, 1), "hv_window": v["hv_window"], "lookback": v["lookback"]}
    if pct >= v["extreme_pct"]:
        return "extreme", ev
    if pct >= v["high_pct"]:
        return "high", ev
    if pct < v["low_pct"]:
        return "low", ev
    return "normal", ev


def classify_participation(grouped: pd.DataFrame | None, benchmark: str, cfg: dict) -> tuple[str, dict]:
    p = cfg["participation"]
    if grouped is None or grouped.empty:
        return "unknown", {"reason": "breadth_unavailable"}
    b = grouped[grouped["T"] == benchmark]
    if b.empty:
        return "unknown", {"reason": f"{benchmark}_missing_from_grouped"}
    move = float(b["c"].iloc[0] - b["o"].iloc[0])
    sign = (move > 0) - (move < 0)
    if sign == 0:
        return "unknown", {"reason": "benchmark_flat_session"}
    u = p["universe"]
    uni = F.filter_universe(grouped, u["min_price"], u["min_volume"], u["exclude_suffix_regex"])
    ev = {"benchmark_session_sign": sign, "universe_size": len(uni), "proxy": "close_vs_open"}
    if len(uni) < p["min_universe"]:
        return "unknown", ev | {"reason": "universe_too_small"}
    names = F.name_agreement(uni, sign)
    sec = F.sector_agreement(grouped, p["sectors"], sign)
    ev |= {"name_agreement": _r(names, 3)}
    if sec is None:
        return "unknown", ev | {"reason": "sector_etfs_missing"}
    sec_frac, sec_n = sec
    ev |= {"sector_agreement": _r(sec_frac, 3), "sectors_found": sec_n}
    if names >= p["broad"]["name_agree_min"] and sec_frac >= p["broad"]["sector_agree_min"]:
        return "broad", ev
    if names <= p["narrow"]["name_agree_max"] or sec_frac <= p["narrow"]["sector_agree_max"]:
        return "narrow", ev
    return "mixed", ev


def transition(current: dict, history: list[dict], confirm_sessions: int) -> dict:
    """Track (direction, structure) changes against stored history (oldest first, prior sessions only)."""
    key = (current["direction"], current["structure"])
    confirmed = None
    for h in history:
        if h.get("transition", {}).get("confirmed"):
            confirmed = tuple(h["transition"]["confirmed"])
    run = 1
    for h in reversed(history):
        if (h["state"]["direction"], h["state"]["structure"]) == key:
            run += 1
        else:
            break
    if confirmed == key:
        return {
            "previous_confirmed": list(confirmed),
            "candidate": None,
            "candidate_sessions": 0,
            "confirmed": list(key),
            "status": "stable",
        }
    status = "confirmed" if run >= confirm_sessions else "pending"
    return {
        "previous_confirmed": list(confirmed) if confirmed else None,
        "candidate": list(key),
        "candidate_sessions": run,
        "confirmed": list(key) if status == "confirmed" else (list(confirmed) if confirmed else None),
        "status": status,
    }


def model_book(state: dict, trans: dict, events: list[dict]) -> list[dict]:
    out = []
    d, s = state["direction"], state["structure"]
    if s == "trend" and d == "up":
        out.append({"id": "M1", "name": "trend up", "rule": "direction=up AND structure=trend"})
    if s == "trend" and d == "down":
        out.append({"id": "M2", "name": "trend down", "rule": "direction=down AND structure=trend"})
    if s == "range":
        out.append({"id": "M3", "name": "range", "rule": "structure=range"})
    prev = trans.get("previous_confirmed")
    if (
        trans.get("status") == "confirmed"
        and prev
        and prev[0] in ("up", "down")
        and d in ("up", "down")
        and prev[0] != d
    ):
        out.append({"id": "M4", "name": "reversal", "rule": f"confirmed direction flip {prev[0]}->{d}"})
    if d in ("up", "down") and state["participation"] == "narrow":
        out.append(
            {"id": "M5", "name": "divergence", "rule": f"direction={d} AND participation=narrow", "overlay": True}
        )
    if events:
        out.append({"id": "M6", "name": "event", "rule": f"{len(events)} event flag(s) supplied", "overlay": True})
    return out


def playbook_policy(state: dict, cfg: dict) -> list[dict]:
    out = []
    for name, rule in (cfg.get("playbooks") or {}).items():
        reasons, decision = [], "eligible"
        for dim, vals in (rule.get("blocked_when") or {}).items():
            if state.get(dim) in vals:
                decision = "blocked"
                reasons.append(f"blocked_when:{dim}={state.get(dim)}")
        for dim, vals in (rule.get("requires") or {}).items():
            if state.get(dim) not in vals:
                decision = "blocked"
                reasons.append(f"requires:{dim} in {vals}, got {state.get(dim)}")
        if decision != "blocked":
            for dim, vals in (rule.get("prefers") or {}).items():
                if state.get(dim) not in vals:
                    decision = "conditional"
                    reasons.append(f"prefers:{dim} in {vals}, got {state.get(dim)}")
        out.append({"playbook": name, "decision": decision, "reasons": reasons or ["all_conditions_met"]})
    return out


def assess(
    as_of: dt.date,
    cfg: dict,
    bench_bars: pd.DataFrame | None,
    confirm_bars: pd.DataFrame | None,
    grouped: pd.DataFrame | None,
    grouped_date: dt.date | None = None,
    events: list[dict] | None = None,
    history: list[dict] | None = None,
    provenance: dict | None = None,
    extra_evidence: dict | None = None,
) -> dict:
    events = events or []
    bench_name, confirm_name = cfg["benchmarks"]["direction"], cfg["benchmarks"]["confirm"]
    bars = _prior_bars(bench_bars, as_of)
    cbars = _prior_bars(confirm_bars, as_of)
    if grouped_date is not None and grouped_date >= as_of:
        grouped = None  # a session on/after as_of is future data for this assessment

    direction, ev_dir = classify_direction(bars, cfg)
    structure, ev_str = classify_structure(bars, cfg)
    volatility, ev_vol = classify_volatility(bars, cfg)
    participation, ev_par = classify_participation(grouped, bench_name, cfg)
    confirm_dir, ev_conf = classify_direction(cbars, cfg)

    state = {
        "direction": direction,
        "structure": structure,
        "volatility": volatility,
        "participation": participation,
        "liquidity": "unknown",
        "event_flags": events,
    }

    dq_reasons = []
    last_bar = bars["date"].iloc[-1] if not bars.empty else None
    if last_bar is None:
        dq_reasons.append(f"{bench_name}_bars_unavailable")
    elif (as_of - last_bar).days > cfg["data_quality"]["max_bar_age_days"]:
        dq_reasons.append(f"{bench_name}_bars_stale(last={last_bar})")
    unknown = [k for k in ("direction", "structure", "volatility", "participation") if state[k] == "unknown"]
    dq_reasons += [f"{k}_unknown" for k in unknown]
    dq_reasons.append("liquidity_unknown(no_quote_data)")
    if last_bar is None or direction == "unknown":
        data_quality = "unusable"
    elif unknown or any("stale" in r for r in dq_reasons):
        data_quality = "degraded"
    else:
        data_quality = "valid"
    state["data_quality"] = data_quality

    conflicts = []
    if confirm_dir not in ("unknown", direction) and direction != "unknown":
        conflicts.append(f"{confirm_name} direction={confirm_dir} vs {bench_name} direction={direction}")

    trans = transition(state, history or [], cfg["transitions"]["confirm_sessions"])

    inputs_fingerprint = {
        "as_of": str(as_of),
        "bench_last": str(last_bar),
        "bench_n": len(bars),
        "bench_close": None if bars.empty else round(float(bars["close"].iloc[-1]), 4),
        "grouped_date": None if grouped is None else str(grouped_date),
        "grouped_n": None if grouped is None else len(grouped),
        "events": events,
        "config_version": cfg["version"],
    }
    assessment_id = hashlib.sha1(
        json.dumps({"i": inputs_fingerprint, "s": state, "t": trans}, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    return {
        "schema_version": SCHEMA_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "configuration_version": cfg["version"],
        "configuration_status": cfg["status"],
        "assessment_id": assessment_id,
        "as_of": str(as_of),
        "scope": {"kind": "market", "benchmark": bench_name, "confirm": confirm_name, "timeframe": "daily_pre_session"},
        "state": state,
        "evidence": {
            "direction": ev_dir,
            "structure": ev_str,
            "volatility": ev_vol,
            "participation": ev_par,
            f"confirm_{confirm_name}": {"direction": confirm_dir, **ev_conf},
            **(extra_evidence or {}),
        },
        "conflicts": conflicts,
        "data_quality_reasons": dq_reasons,
        "transition": trans,
        "model_book": model_book(state, trans, events),
        "playbooks": playbook_policy(state, cfg),
        "provenance": provenance or {},
        "note": "Regime describes conditions and gates playbook eligibility. It is never a trade signal.",
    }
