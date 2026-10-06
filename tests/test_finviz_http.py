"""Finviz rate limit, backoff, universe cache and local screens.

Nothing here touches the network: ``requests.Session.request`` and ``finviz_get`` are faked,
time is injected, and the state dir is redirected to ``tmp_path``.
"""

from __future__ import annotations

import pandas as pd
import pytest
import requests
from click.testing import CliRunner

from tradekit.data import finviz_elite
from tradekit.data.finviz_elite import (
    FinvizEliteConfig,
    FinvizEliteProvider,
    most_active,
    screen_universe,
    top_gainers,
    top_losers,
    unusual_volume,
)
from tradekit.data.finviz_http import (
    BackoffPolicy,
    FinvizSession,
    FinvizThrottled,
    RateLimiter,
    looks_like_html,
    redact,
)

URL = "https://elite.finviz.com/export.ashx?v=152&f=sh_price_o1&auth=SECRET123"

CSV = (
    "No.,Ticker,Company,Price,Change,Volume,Relative Volume,Average Volume\n"
    "1,AAA,Alpha,10.00,5.00%,1000000,3.1,500\n"
    "2,BBB,Beta,2.50,12.00%,9000000,8.0,1200\n"
    "3,CCC,Gamma,50.00,-4.00%,400000,0.9,2000\n"
    "4,DDD,Delta,25.00,1.50%,5000000,2.2,150\n"
    "5,EEE,Echo,7.00,-,100,-,0.01\n"
)


def _resp(status: int = 200, text: str = "ok", headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = text.encode()
    r.headers.update(headers or {})
    r.url = URL
    return r


class _NoWaitLimiter(RateLimiter):
    def __init__(self):
        super().__init__(max_requests=10**6, window=1.0)
        self.calls = 0

    def acquire(self) -> float:
        self.calls += 1
        return 0.0


@pytest.fixture
def fake_http(monkeypatch):
    """Queue responses (or exceptions) for requests.Session.request; returns the queue."""
    queue: list = []

    def fake_request(self, method, url, *args, **kwargs):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return queue


def _session(max_attempts: int = 4) -> tuple[FinvizSession, list[float]]:
    sleeps: list[float] = []
    s = FinvizSession(
        policy=BackoffPolicy(max_attempts=max_attempts, base=2.0, factor=2.0, cap=60.0, jitter=0.0),
        limiter=_NoWaitLimiter(),
        sleep=sleeps.append,
    )
    return s, sleeps


# ----- backoff -----


def test_backoff_grows_and_caps():
    p = BackoffPolicy(base=2.0, factor=2.0, cap=10.0, jitter=0.0)
    assert [p.delay(n) for n in range(1, 6)] == [2.0, 4.0, 8.0, 10.0, 10.0]


def test_backoff_jitter_bounded():
    p = BackoffPolicy(base=2.0, factor=2.0, cap=60.0, jitter=0.25)
    assert p.delay(1, rng=lambda: 1.0) == pytest.approx(2.5)
    assert p.delay(1, rng=lambda: 0.0) == pytest.approx(2.0)


@pytest.mark.parametrize("status", [403, 429, 503])
def test_retryable_status_then_success(fake_http, status):
    s, sleeps = _session()
    fake_http.extend([_resp(status), _resp(status), _resp(200, "Ticker\nAAA\n")])
    r = s.get(URL, expect_csv=True)
    assert r.status_code == 200
    assert sleeps == [2.0, 4.0]  # incremental
    assert s.limiter.calls == 3  # every attempt draws on the rate budget


def test_retry_after_overrides_delay(fake_http):
    s, sleeps = _session()
    fake_http.extend([_resp(429, headers={"Retry-After": "7"}), _resp(200, "Ticker\n")])
    s.get(URL, expect_csv=True)
    assert sleeps == [7.0]


def test_connection_error_retried(fake_http):
    s, sleeps = _session()
    fake_http.extend([requests.ConnectionError("reset"), _resp(200, "Ticker\n")])
    assert s.get(URL).status_code == 200
    assert sleeps == [2.0]


def test_html_for_csv_is_throttle(fake_http):
    s, sleeps = _session()
    page = "<!DOCTYPE html><html><body>Too many requests</body></html>"
    fake_http.extend([_resp(200, page, {"Content-Type": "text/html"}), _resp(200, "Ticker\nAAA\n")])
    assert s.get(URL, expect_csv=True).text.startswith("Ticker")
    assert sleeps == [2.0]


def test_html_ok_when_html_expected(fake_http):
    s, sleeps = _session()
    fake_http.append(_resp(200, "<html>news</html>", {"Content-Type": "text/html"}))
    assert s.get(URL).status_code == 200
    assert sleeps == []


def test_exhausted_raises_typed_error_with_redacted_url(fake_http):
    s, sleeps = _session(max_attempts=3)
    fake_http.extend([_resp(403), _resp(403), _resp(403)])
    with pytest.raises(FinvizThrottled) as exc:
        s.get(URL, expect_csv=True)
    assert exc.value.attempts == 3
    assert "HTTP 403" in str(exc.value)
    assert "SECRET123" not in str(exc.value)
    assert sleeps == [2.0, 4.0]  # no sleep after the final attempt


def test_non_retryable_4xx_returned_not_retried(fake_http):
    s, sleeps = _session()
    fake_http.append(_resp(404))
    assert s.get(URL).status_code == 404
    assert sleeps == []


def test_redact():
    assert redact(URL).endswith("auth=***")
    assert redact("https://x/?auth=abc&t=AAPL") == "https://x/?auth=***&t=AAPL"


def test_looks_like_html():
    assert looks_like_html(_resp(200, "  <html><body/></html>"))
    assert not looks_like_html(_resp(200, "No.,Ticker\n1,AAA\n"))


# ----- rate limiter -----


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_rate_limiter_spaces_requests(tmp_path):
    clock = _Clock()
    lim = RateLimiter(max_requests=10, window=5.0, path=tmp_path / "rl.json", clock=clock, sleep=clock.sleep)
    waits = [lim.acquire() for _ in range(10)]
    assert waits == [0.0] * 10
    assert lim.acquire() == pytest.approx(5.01)  # 11th waits for the window to roll


def test_rate_limiter_shared_across_instances(tmp_path):
    """Two limiters on one file stand in for two processes on one node."""
    clock = _Clock()
    path = tmp_path / "rl.json"
    a = RateLimiter(max_requests=10, window=5.0, path=path, clock=clock, sleep=clock.sleep)
    b = RateLimiter(max_requests=10, window=5.0, path=path, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        a.acquire()
        b.acquire()
    assert b.acquire() > 0  # the shared budget is spent


# ----- universe cache + local screens -----


@pytest.fixture
def elite(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADEKIT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        FinvizEliteProvider, "_ttl_seconds", staticmethod(lambda m: 600.0 if m is None else float(m) * 60)
    )
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _resp(200, CSV)

    monkeypatch.setattr(finviz_elite, "finviz_get", fake_get)
    return FinvizEliteProvider(FinvizEliteConfig(auth_token="SECRET123")), calls


def test_universe_fetched_once_then_cached(elite):
    fp, calls = elite
    a = fp.get_universe()
    b = fp.get_universe()
    assert len(calls) == 1
    assert list(a["Ticker"]) == list(b["Ticker"]) == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert "f=sh_price_o1" in calls[0]


def test_universe_refresh_and_expiry(elite):
    fp, calls = elite
    fp.get_universe()
    fp.get_universe(refresh=True)
    fp.get_universe(ttl_minutes=0)
    assert len(calls) == 3


def test_universe_without_ticker_column_raises(elite, monkeypatch):
    fp, _ = elite
    monkeypatch.setattr(finviz_elite, "finviz_get", lambda url, **k: _resp(200, "foo,bar\n1,2\n"))
    with pytest.raises(ValueError, match="no Ticker column"):
        fp.get_universe(refresh=True)


def test_get_quotes_served_from_universe_cache(elite):
    fp, calls = elite
    fp.get_universe()
    q = fp.get_quotes(["aaa", "DDD"])
    assert len(calls) == 1  # no second request
    assert sorted(q["Ticker"]) == ["AAA", "DDD"]
    assert q.loc[q["Ticker"] == "AAA", "Change"].iloc[0] == pytest.approx(5.0)  # % coerced like the network path


def test_get_quotes_falls_through_when_ticker_missing(elite):
    fp, calls = elite
    fp.get_universe()
    fp.get_quotes(["AAA", "ZZZ"])
    assert len(calls) == 2 and "t=AAA,ZZZ" in calls[1]


def test_get_quotes_falls_through_without_cache(elite):
    fp, calls = elite
    fp.get_quotes(["AAA"])
    assert len(calls) == 1 and "t=AAA" in calls[0]


def _universe() -> pd.DataFrame:
    import io

    return pd.read_csv(io.StringIO(CSV))


def test_top_gainers_local():
    g = top_gainers(_universe(), min_price=5.0)
    assert list(g["Ticker"]) == ["AAA", "DDD"]  # BBB under $5, CCC negative, EEE blank change dropped
    assert list(top_gainers(_universe(), min_price=1.0)["Ticker"]) == ["BBB", "AAA", "DDD"]


def test_top_losers_local():
    assert list(top_losers(_universe(), min_price=1.0)["Ticker"]) == ["CCC"]


def test_unusual_volume_and_most_active_local():
    assert list(unusual_volume(_universe(), min_price=1.0, min_rvol=2.0)["Ticker"]) == ["BBB", "AAA", "DDD"]
    assert list(most_active(_universe(), min_price=1.0, limit=2)["Ticker"]) == ["BBB", "DDD"]


def test_screen_missing_column_is_loud():
    with pytest.raises(KeyError):
        screen_universe(_universe().drop(columns=["Relative Volume"]), min_rvol=2.0)


# ----- callers -----


def test_finviz_gainers_uses_universe_when_token_set(monkeypatch, elite):
    from tradekit.screener import premarket

    fp, calls = elite
    monkeypatch.setattr(FinvizEliteConfig, "from_env", classmethod(lambda cls: FinvizEliteConfig(auth_token="x")))
    a = premarket.finviz_gainers(min_price=5.0)
    b = premarket.finviz_gainers(min_price=3.0)  # second screen, same window
    assert list(a["Ticker"]) == ["AAA", "DDD"]
    assert len(b) == 2
    assert len(calls) == 1


def test_finviz_gainers_falls_back_to_scraper(monkeypatch):
    from tradekit.screener import premarket

    monkeypatch.setattr(FinvizEliteConfig, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(
        premarket.FinvizProvider, "get_top_gainers", lambda self, min_price: pd.DataFrame({"Ticker": ["Q"]})
    )
    assert list(premarket.finviz_gainers(min_price=5.0)["Ticker"]) == ["Q"]


def test_scraper_screen_reraises_throttle(monkeypatch):
    from tradekit.data import finviz

    def boom(self, *a, **k):
        raise FinvizThrottled(URL, 5, "HTTP 403")

    monkeypatch.setattr(finviz.Overview, "screener_view", boom)
    with pytest.raises(FinvizThrottled):
        finviz.FinvizProvider().get_top_gainers()


def test_cli_reports_throttle_as_unavailable(monkeypatch):
    from tradekit import cli as cli_mod
    from tradekit.screener import premarket

    def boom(**kwargs):
        raise FinvizThrottled(URL, 5, "HTTP 403")

    monkeypatch.setattr(premarket, "scan_previous_movers", boom)
    result = CliRunner().invoke(cli_mod.cli, ["second-day"])
    assert result.exit_code == 3
    assert "UNAVAILABLE" in result.output
    assert "SECRET123" not in result.output


def test_min_avg_volume_uses_thousands():
    u = _universe()
    u["Average Volume"] = [500.0, 50.0, 2000.0, 150.0, 0.004]  # thousands, as the export reports
    g = top_gainers(u, min_price=1.0, min_avg_volume=100_000)
    assert list(g["Ticker"]) == ["AAA", "DDD"]  # BBB (50K avg) dropped
    assert "_avg_vol_shares" not in g.columns


# ----- api-403 event log -----


def test_403_recorded_redacted(fake_http, monkeypatch, tmp_path):
    import json

    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("API_ERROR_LOG", str(log))
    s, _ = _session(max_attempts=2)
    fake_http.extend([_resp(403), _resp(200, "Ticker\n")])
    s.get("https://elite.finviz.com/export.ashx?v=152&t=AAPL&auth=SECRET123", expect_csv=True)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(events) == 1
    ev = events[0]
    assert (ev["app"], ev["provider"], ev["status"], ev["path"]) == ("tradekit", "finviz", 403, "/export.ashx")
    assert ev["detail"] == "attempt 1/2"
    assert "SECRET123" not in log.read_text() and "AAPL" not in log.read_text()


def test_429_not_recorded(fake_http, monkeypatch, tmp_path):
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("API_ERROR_LOG", str(log))
    s, _ = _session(max_attempts=2)
    fake_http.extend([_resp(429), _resp(200, "Ticker\n")])
    s.get(URL, expect_csv=True)
    assert not log.exists()


def test_normalize_paths():
    from tradekit.data.api_errors import normalize

    assert normalize("https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/2026-10-01/2026-10-05?apiKey=x") == (
        "api.polygon.io",
        "/v2/aggs/ticker/{ticker}/range/1/day/{date}/{date}",
    )
