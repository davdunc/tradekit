"""Dimensional regime engine: offline, synthetic bars, injected config. Nothing touches Massive."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from tradekit.regime import RegimeConfigError, assess, load_config, store
from tradekit.regime.classify import playbook_policy, transition

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "regime.example.yaml"
AS_OF = dt.date(2026, 10, 6)


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "regime.yaml"
    shutil.copy(EXAMPLE, p)
    return load_config(p)


def _bars(closes, end=dt.date(2026, 10, 5)):
    n = len(closes)
    dates = pd.bdate_range(end=end, periods=n).date
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": dates, "open": c, "high": c * 1.004, "low": c * 0.996, "close": c, "volume": 1e6})


def _trend_up(n=300):
    rng = np.random.default_rng(1)
    return _bars(100 * np.exp(np.cumsum(0.004 + rng.normal(0, 0.002, n))))


def _range(n=300):
    return _bars([100 + 1.5 * (-1) ** i for i in range(n)])  # zig-zag: no net move over an even window


def _grouped(up_frac: float, sectors_up: int, spy_up: bool = True, n: int = 800) -> pd.DataFrame:
    k = int(n * up_frac)
    rows = [{"T": f"S{i:04d}", "o": 10.0, "c": 10.5 if i < k else 9.5, "v": 1e6} for i in range(n)]
    secs = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    rows += [{"T": s, "o": 50.0, "c": 51.0 if i < sectors_up else 49.0, "v": 1e6} for i, s in enumerate(secs)]
    rows.append({"T": "SPY", "o": 700.0, "c": 705.0 if spy_up else 695.0, "v": 5e7})
    return pd.DataFrame(rows)


def run(cfg, bench, grouped=None, **kw):
    return assess(AS_OF, cfg, bench, bench, grouped, dt.date(2026, 10, 5), **kw)


# ----- config contract -----


def test_missing_config_is_an_error(tmp_path):
    with pytest.raises(RegimeConfigError, match="No regime config"):
        load_config(tmp_path / "nope.yaml")


def test_missing_key_is_an_error(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(EXAMPLE.read_text().replace("trend_min: 0.40", ""))
    with pytest.raises(RegimeConfigError, match="structure.trend_min"):
        load_config(p)


def test_example_config_is_experimental(cfg):
    assert cfg["status"] == "experimental"
    assert "proposed" in cfg["version"]


# ----- dimensions -----


def test_trend_up_broad(cfg):
    a = run(cfg, _trend_up(), _grouped(0.75, 9))
    s = a["state"]
    assert (s["direction"], s["structure"], s["participation"]) == ("up", "trend", "broad")
    assert s["liquidity"] == "unknown"
    assert any(m["id"] == "M1" for m in a["model_book"])


def test_range_structure(cfg):
    a = run(cfg, _range(), _grouped(0.5, 6))
    assert a["state"]["structure"] == "range"
    assert any(m["id"] == "M3" for m in a["model_book"])


def test_narrow_participation_flags_divergence(cfg):
    a = run(cfg, _trend_up(), _grouped(0.30, 3))
    assert a["state"]["participation"] == "narrow"
    assert any(m["id"] == "M5" for m in a["model_book"])


def test_missing_breadth_is_unknown_not_guessed(cfg):
    a = run(cfg, _trend_up(), None)
    assert a["state"]["participation"] == "unknown"
    assert a["state"]["data_quality"] == "degraded"
    assert "participation_unknown" in a["data_quality_reasons"]


def test_no_bars_is_unusable(cfg):
    a = run(cfg, None, None)
    assert a["state"]["direction"] == "unknown"
    assert a["state"]["data_quality"] == "unusable"


def test_short_history_volatility_unknown(cfg):
    a = run(cfg, _trend_up(60), _grouped(0.7, 8))
    assert a["state"]["volatility"] == "unknown"
    assert a["state"]["direction"] == "up"  # other dimensions still classify


def test_events_only_from_input(cfg):
    assert run(cfg, _trend_up(), _grouped(0.7, 8))["state"]["event_flags"] == []
    a = run(cfg, _trend_up(), _grouped(0.7, 8), events=[{"type": "fomc", "label": "FOMC minutes"}])
    assert any(m["id"] == "M6" for m in a["model_book"])


def test_universe_filter_keeps_real_tickers():
    from tradekit.regime.features import filter_universe

    g = pd.DataFrame(
        {"T": ["SNOW", "UBER", "AAPL", "ARBEW", "ABCDU", "BCATr", "BRK.B"], "o": 10.0, "c": 10.0, "v": 1e6}
    )
    rx = load_config(EXAMPLE)["participation"]["universe"]["exclude_suffix_regex"]
    assert list(filter_universe(g, 5, 1, rx)["T"]) == ["SNOW", "UBER", "AAPL"]


# ----- determinism + no future data -----


def test_identical_inputs_identical_output(cfg):
    a = run(cfg, _trend_up(), _grouped(0.7, 8))
    b = run(cfg, _trend_up(), _grouped(0.7, 8))
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_future_bars_cannot_change_past_assessment(cfg):
    base = _trend_up()
    future = pd.concat([base, _bars([1.0, 1.0, 1.0], end=dt.date(2026, 10, 9))], ignore_index=True)
    assert run(cfg, base)["assessment_id"] == run(cfg, future)["assessment_id"]


def test_grouped_on_or_after_as_of_is_ignored(cfg):
    a = assess(AS_OF, cfg, _trend_up(), _trend_up(), _grouped(0.7, 8), AS_OF)
    assert a["state"]["participation"] == "unknown"


def test_stale_bars_degrade_quality(cfg):
    a = assess(dt.date(2026, 10, 30), cfg, _trend_up(), _trend_up(), None)
    assert any("stale" in r for r in a["data_quality_reasons"])


def test_never_a_trade_signal(cfg):
    a = run(cfg, _trend_up(), _grouped(0.7, 8))
    blob = json.dumps(a).lower()
    assert "never a trade signal" in blob
    assert not any(k in a for k in ("signal", "order", "entry", "side"))


# ----- transitions + store -----


def _hist(direction, structure, confirmed=None):
    return {"state": {"direction": direction, "structure": structure}, "transition": {"confirmed": confirmed}}


def test_transition_pending_then_confirmed():
    cur = {"direction": "down", "structure": "trend"}
    prior = [_hist("up", "trend", ["up", "trend"])]
    t1 = transition(cur, prior, confirm_sessions=2)
    assert (t1["status"], t1["confirmed"], t1["candidate_sessions"]) == ("pending", ["up", "trend"], 1)
    t2 = transition(cur, [*prior, _hist("down", "trend", ["up", "trend"])], confirm_sessions=2)
    assert (t2["status"], t2["confirmed"]) == ("confirmed", ["down", "trend"])


def test_transition_stable():
    t = transition({"direction": "up", "structure": "trend"}, [_hist("up", "trend", ["up", "trend"])], 2)
    assert t["status"] == "stable"


def test_store_never_overwrites(cfg, tmp_path):
    a = run(cfg, _trend_up(), _grouped(0.7, 8))
    p = store.save(a, base=tmp_path)
    p.write_text('{"tampered": true}')
    store.save(a, base=tmp_path)
    assert json.loads(p.read_text()) == {"tampered": True}  # existing record untouched


def test_history_excludes_as_of_and_later(cfg, tmp_path):
    for day in ("2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"):
        a = run(cfg, _trend_up(), _grouped(0.7, 8)) | {"as_of": day}
        store.save(a | {"assessment_id": day}, base=tmp_path)
    h = store.history(AS_OF, base=tmp_path)
    assert [x["as_of"] for x in h] == ["2026-10-02", "2026-10-05"]


# ----- policy -----


def test_playbook_policy_reasons(cfg):
    state = {"direction": "up", "structure": "range", "volatility": "normal", "participation": "narrow"}
    got = {p["playbook"]: p for p in playbook_policy(state, cfg)}
    assert got["fashionably_late_long"]["decision"] == "conditional"
    assert "prefers:structure" in got["fashionably_late_long"]["reasons"][0]
    assert got["fashionably_late_short"]["decision"] == "blocked"
    assert got["range_edge_fade"]["decision"] == "eligible"


def test_extreme_volatility_blocks(cfg):
    state = {"direction": "up", "structure": "trend", "volatility": "extreme", "participation": "broad"}
    got = {p["playbook"]: p["decision"] for p in playbook_policy(state, cfg)}
    assert got["fashionably_late_long"] == "blocked"


# ----- Massive client -----


def _resp(status, payload=None, url="https://api.polygon.io/x?apiKey=SECRET"):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload or {}).encode()
    r.url = url
    r.reason = "Forbidden" if status == 403 else "OK"
    return r


class _Lim:
    def __init__(self):
        self.n = 0

    def acquire(self):
        self.n += 1
        return 0.0


def test_massive_403_recorded_and_not_retried(monkeypatch, tmp_path):
    from tradekit.data.massive_rest import MassiveError, MassiveREST

    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("API_ERROR_LOG", str(log))
    calls = []
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **k: (
            calls.append(url) or _resp(403, url="https://api.polygon.io/v3/snapshot/options/SPY?apiKey=SECRET")
        ),
    )
    c = MassiveREST(api_key="SECRET", limiter=_Lim())
    with pytest.raises(MassiveError) as e:
        c.grouped_daily(dt.date(2026, 10, 5))
    assert len(calls) == 1  # no retry against the feed
    assert "SECRET" not in str(e.value)
    ev = json.loads(log.read_text())
    assert (ev["provider"], ev["status"], ev["app"]) == ("massive", 403, "tradekit")
    assert "SECRET" not in log.read_text()


def test_daily_bar_dates_are_new_york_sessions(monkeypatch):
    from tradekit.data.massive_rest import MassiveREST

    # 2026-10-05 00:00 America/New_York = 04:00 UTC
    t = int(dt.datetime(2026, 10, 5, 4, 0, tzinfo=dt.UTC).timestamp() * 1000)
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **k: _resp(200, {"results": [{"t": t, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]}),
    )
    df = MassiveREST(api_key="k", limiter=_Lim()).daily_bars("SPY", dt.date(2026, 10, 1), dt.date(2026, 10, 5))
    assert df["date"].iloc[0] == dt.date(2026, 10, 5)


def test_fetch_inputs_uses_three_requests(monkeypatch, cfg):
    from tradekit.data.massive_rest import MassiveREST
    from tradekit.regime.sources import fetch_inputs

    t = int(dt.datetime(2026, 10, 5, 4, 0, tzinfo=dt.UTC).timestamp() * 1000)
    bars = {"results": [{"t": t, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]}
    grouped = {"results": [{"T": "SPY", "o": 1, "c": 2, "v": 3}]}
    monkeypatch.setattr(requests, "get", lambda url, **k: _resp(200, grouped if "grouped" in url else bars))
    lim = _Lim()
    out = fetch_inputs(AS_OF, cfg, MassiveREST(api_key="k", limiter=lim))
    assert out["requests"] == 3 and lim.n == 3
    assert out["grouped_date"] == dt.date(2026, 10, 5)


# ----- glance -----


def test_glance_range_day(cfg):
    from tradekit.regime.classify import glance

    state = {"direction": "neutral", "structure": "range", "volatility": "normal", "participation": "mixed"}
    g = glance(state, [], playbook_policy(state, cfg), cfg["status"])
    assert g["headline"] == "🔁 RANGE DAY — fade the edges, trade catalysts"
    assert g["chips"] == ["→ Neutral", "🔁 Range", "● Normal vol", "◐ Mixed"]
    assert "range edge fade" in g["trade"]
    assert any(c.startswith("fashionably late long (structure is range") for c in g["careful"])
    assert g["off"] == []
    assert g["provisional"] is True


def test_glance_extreme_vol_moves_plays_off(cfg):
    from tradekit.regime.classify import glance

    state = {"direction": "neutral", "structure": "range", "volatility": "extreme", "participation": "mixed"}
    g = glance(state, [], playbook_policy(state, cfg), cfg["status"])
    assert "smallest size" in g["headline"]
    assert any(o.startswith("range edge fade (volatility is extreme") for o in g["off"])


def test_glance_trend_up_and_conflict(cfg):
    down = _bars(list(reversed(_trend_up()["close"].tolist())))
    a = assess(AS_OF, cfg, _trend_up(), down, _grouped(0.75, 9), dt.date(2026, 10, 5))
    g = a["glance"]
    assert g["headline"].startswith("📈 TREND UP")
    assert any(c.startswith("⚠ QQQ down vs SPY up") for c in g["chips"])


def test_glance_unclear_when_no_data(cfg):
    assert run(cfg, None)["glance"]["headline"].startswith("❔ UNCLEAR")
