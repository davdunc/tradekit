"""MassiveProvider on Massive's REST API (#52: the MCP server's CSV was parsed as JSON).

Shapes are the live stock snapshot and aggregates responses seen on 2026-11-22 (Friday's data),
served through ``requests.get`` fakes so no key or network is needed.
"""

import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from tradekit.config import ET
from tradekit.data import massive_rest
from tradekit.data.massive import MassiveProvider, in_premarket
from tradekit.data.massive_rest import NY, MassiveREST

BASE = "https://massive.test"
SAT = dt.datetime(2026, 11, 21, 12, 0, tzinfo=ET)

#: AAPL's snapshot as returned live: no lastTrade/lastQuote on this plan; min is Friday 19:59 post-market.
SNAPSHOT = {
    "status": "OK",
    "ticker": {
        "ticker": "AAPL",
        "day": {"o": 352.865, "h": 354.34, "l": 340.56, "c": 342.56, "v": 81187216.0},
        "min": {"t": 1795222740000, "c": 344.87, "av": 81187216},
        "prevDay": {"c": 349.21, "v": 40906860.0},
        "updated": 1795222800000000000,
    },
}


def bar(day, close, volume):
    t = int(dt.datetime.combine(day, dt.time(0), NY).timestamp() * 1000)
    return {"t": t, "o": close, "h": close, "l": close, "c": close, "v": volume}


def provider(monkeypatch, pages, now=SAT):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        for prefix, payload in pages.items():
            if prefix in url:
                return SimpleNamespace(status_code=200, json=lambda p=payload: p, url=url, reason="OK")
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(massive_rest.requests, "get", get)
    rest = MassiveREST(api_key="k", limiter=SimpleNamespace(acquire=lambda: None), base_url=BASE)
    return MassiveProvider(rest=rest, clock=lambda: now), calls


def daily(days):
    start = dt.date(2026, 10, 19)
    rows = [bar(start + dt.timedelta(days=i), 340 + i, 1_000_000 * (i + 1)) for i in range(days)]
    return {"results": rows}


def test_quote_from_the_snapshot_with_a_real_average_volume(monkeypatch):
    p, _ = provider(monkeypatch, {"/v2/snapshot/": SNAPSHOT, "/range/1/day/": daily(25)})
    q = p.get_quote("aapl")
    assert (q["ticker"], q["price"], q["prev_close"], q["open"]) == ("AAPL", 342.56, 349.21, 352.865)
    assert q["volume"] == 81187216.0
    # the last 20 completed sessions of 25 (all before Saturday): volumes 6M..25M
    assert q["avg_volume"] == pytest.approx(sum(range(6, 26)) / 20 * 1_000_000)
    assert q["market_time"] == pytest.approx(1795222800.0)


def test_average_volume_leaves_out_todays_partial_session(monkeypatch):
    today = dt.date(2026, 11, 20)
    rows = [bar(today - dt.timedelta(days=1), 1, 100), bar(today, 1, 5)]
    p, _ = provider(
        monkeypatch,
        {"/v2/snapshot/": SNAPSHOT, "/range/1/day/": {"results": rows}},
        now=dt.datetime(2026, 11, 20, 11, 0, tzinfo=ET),
    )
    assert p.get_quote("AAPL")["avg_volume"] == 100


def test_history_is_indexed_by_new_york_time_and_follows_next_url(monkeypatch):
    first = {"results": daily(2)["results"], "next_url": f"{BASE}/v2/aggs/ticker/AAPL/range/5/minute/next?cursor=1"}
    second = {"results": [bar(dt.date(2026, 10, 21), 400, 7)]}
    p, calls = provider(monkeypatch, {"/range/5/minute/next": second, "/range/5/minute/": first})
    df = p.get_history("AAPL", period="5d", interval="5m")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 3 and str(df.index.tz) == "America/New_York"
    assert df["close"].iloc[-1] == 400
    assert calls[0].startswith(f"{BASE}/v2/aggs/ticker/AAPL/range/5/minute/2026-11-16/2026-11-21")


def test_empty_history_keeps_the_columns(monkeypatch):
    p, _ = provider(monkeypatch, {"/range/1/day/": {"results": []}})
    df = p.get_history("ZZZZ")
    assert df.empty and list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex)


def test_saturday_has_no_premarket_even_with_a_friday_post_market_minute(monkeypatch):
    p, _ = provider(monkeypatch, {"/v2/snapshot/": SNAPSHOT})
    data = p.get_premarket("AAPL")
    assert (data["has_premarket"], data["pre_price"], data["gap_pct"]) == (False, None, None)


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [((3, 59), False), ((4, 0), True), ((9, 29), True), ((9, 30), False), ((16, 5), False)],
)
def test_premarket_window(hhmm, expected):
    when = dt.datetime(2026, 11, 20, *hhmm, tzinfo=NY)
    assert in_premarket({"t": int(when.timestamp() * 1000)}, now=when) is expected


def test_no_key_is_a_clear_error(monkeypatch):
    from tradekit.config import DataSettings

    monkeypatch.setitem(DataSettings.model_config, "env_file", [])
    for name in ("MASSIVE_API_KEY", "POLYGON_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(massive_rest.MassiveError, match="MASSIVE_API_KEY not set"):
        MassiveProvider()


def test_history_paging_is_capped(monkeypatch):
    loop = {"results": daily(1)["results"], "next_url": f"{BASE}/v2/aggs/ticker/AAPL/range/1/minute/loop"}
    p, calls = provider(monkeypatch, {"/range/1/minute/": loop})
    with pytest.raises(massive_rest.MassiveError, match="more than 3 pages"):
        p._rest.aggs("AAPL", 1, "minute", dt.date(2026, 11, 1), dt.date(2026, 11, 20), max_pages=3)
    assert len(calls) == 3


def test_average_volume_window_covers_twenty_sessions_through_holidays(monkeypatch):
    from tradekit.data import massive as provider_mod

    requested = {}
    p, _ = provider(monkeypatch, {"/v2/snapshot/": SNAPSHOT, "/range/1/day/": daily(25)})
    real = p._rest.daily_bars

    def spy(ticker, start, end):
        requested["days"] = (end - start).days
        return real(ticker, start, end)

    monkeypatch.setattr(p._rest, "daily_bars", spy)
    p.get_quote("AAPL")
    # Thanksgiving + Christmas + New Year's in one window still leaves 20+ weekday sessions.
    assert requested["days"] == provider_mod.AVG_VOLUME_LOOKBACK_DAYS >= 20 * 7 / 5 + 10
