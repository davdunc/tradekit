"""tradekit gex on Massive's options chain snapshot (#55: CBOE blocks automated access).

Offline: ``requests.get`` is faked with pages shaped like Massive's documented
``GET /v3/snapshot/options/{underlyingAsset}`` response, so no key or network is needed.
"""

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from tradekit import cli as cli_mod
from tradekit.analysis.gex import compute_gex
from tradekit.config import ET
from tradekit.data import massive_rest
from tradekit.data.massive_rest import MassiveError, MassiveREST, chain_from_snapshot

SESSION = dt.datetime(2026, 11, 23, 10, 0, tzinfo=ET)
EXP = "2026-12-04"


def contract(strike, right, oi, iv, *, spot=697.49, timeframe="DELAYED", underlying="SPY"):
    occ = f"O:{underlying}261204{right}{int(strike * 1000):08d}"
    item = {
        "details": {
            "contract_type": "call" if right == "C" else "put",
            "expiration_date": EXP,
            "strike_price": strike,
            "ticker": occ,
        },
        "open_interest": oi,
        "greeks": {"delta": 0.5, "gamma": 0.01, "theta": -0.1, "vega": 0.3},
        "underlying_asset": {"ticker": underlying, "price": spot, "timeframe": timeframe},
    }
    if iv is not None:
        item["implied_volatility"] = iv
    return item


# ----- mapping -----


def test_snapshot_maps_to_the_chain_compute_gex_reads():
    chain = chain_from_snapshot([contract(700.0, "C", 1200, 0.18), contract(690.0, "P", 900, 0.21)])
    assert chain["current_price"] == 697.49
    assert chain["freshness"] == "delayed"
    assert chain["options"][0] == {"option": "SPY261204C00700000", "open_interest": 1200, "iv": 0.18}
    assert chain["contracts"] == 2


def test_contract_without_iv_is_kept_with_zero_so_gex_skips_it():
    chain = chain_from_snapshot([contract(500.0, "C", 50, None)])  # deep ITM: Massive omits greeks/IV
    assert chain["options"][0]["iv"] == 0


def test_empty_snapshot_has_no_spot():
    assert chain_from_snapshot([])["current_price"] is None


def test_gex_is_the_same_from_massive_as_from_the_equivalent_cboe_chain():
    results = [contract(700.0, "C", 1200, 0.18), contract(690.0, "P", 900, 0.21)]
    cboe_shape = {
        "current_price": 697.49,
        "options": [
            {"option": "SPY261204C00700000", "open_interest": 1200, "iv": 0.18},
            {"option": "SPY261204P00690000", "open_interest": 900, "iv": 0.21},
        ],
    }
    from_massive = compute_gex(chain_from_snapshot(results), ticker="SPY", now=SESSION)
    from_cboe = compute_gex(cboe_shape, ticker="SPY", now=SESSION)
    assert from_massive["total_net_gex"] == pytest.approx(from_cboe["total_net_gex"])
    assert from_massive["contracts_scanned"] == 2


# ----- paging and safety -----


class FakeResponse:
    def __init__(self, status, payload, url):
        self.status_code, self._payload, self.url, self.reason = status, payload, url, "Forbidden"

    def json(self):
        return self._payload


def fake_requests(monkeypatch, pages, status=200):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        return FakeResponse(status, pages[min(len(calls), len(pages)) - 1], url)

    monkeypatch.setattr(massive_rest.requests, "get", get)
    return calls


#: Not Massive's real host: the client must use whatever config says, never a built-in one (#58).
TEST_BASE = "https://massive.test"


def client():
    return MassiveREST(api_key="test-key", limiter=SimpleNamespace(acquire=lambda: None), base_url=TEST_BASE)


def test_follows_next_url_and_sends_the_key_each_time(monkeypatch):
    nxt = f"{TEST_BASE}/v3/snapshot/options/SPY?cursor=abc"
    calls = fake_requests(
        monkeypatch,
        [{"results": [contract(700.0, "C", 10, 0.2)], "next_url": nxt}, {"results": [contract(690.0, "P", 5, 0.2)]}],
    )
    chain = client().option_chain("spy", expiration_lte=dt.date(2026, 12, 23))
    assert chain["contracts"] == 2 and chain["pages"] == 2
    (url1, params1), (url2, params2) = calls
    assert url1 == f"{TEST_BASE}/v3/snapshot/options/SPY"
    assert params1 == {"limit": 250, "expiration_date.lte": "2026-12-23", "apiKey": "test-key"}
    assert url2 == nxt and params2 == {"apiKey": "test-key"}


def test_index_underlyings_use_the_i_prefix(monkeypatch):
    calls = fake_requests(monkeypatch, [{"results": [contract(6000.0, "C", 10, 0.2, underlying="SPX")]}])
    client().option_chain("SPX")
    assert calls[0][0].endswith("/v3/snapshot/options/I:SPX")


def test_a_next_url_on_another_host_is_refused_before_the_key_is_sent(monkeypatch):
    calls = fake_requests(monkeypatch, [{"results": [], "next_url": "https://evil.example/steal?x=1"}])
    with pytest.raises(MassiveError) as err:
        client().option_chain("SPY")
    assert err.value.status == "bad-host"
    assert len(calls) == 1  # only the first, legitimate request went out


def test_page_cap_stops_a_runaway_cursor(monkeypatch):
    loop = {"results": [contract(700.0, "C", 1, 0.2)], "next_url": f"{TEST_BASE}/v3/snapshot/options/SPY?c=1"}
    fake_requests(monkeypatch, [loop])
    with pytest.raises(MassiveError, match="narrow --max-dte"):
        client().option_chain("SPY", max_pages=3)


def test_not_entitled_is_a_403_error(monkeypatch):
    fake_requests(monkeypatch, [{"status": "NOT_AUTHORIZED"}], status=403)
    with pytest.raises(MassiveError) as err:
        client().option_chain("SPY")
    assert err.value.status == 403


# ----- CLI -----


def gex(*args):
    return CliRunner().invoke(cli_mod.cli, ["gex", *args])


def fake_client(monkeypatch, *, chain=None, error=None):
    class Fake:
        def __init__(self, *a, **k):
            if error is not None and error.status == "no-key":
                raise error

        def option_chain(self, ticker, **kwargs):
            if error is not None:
                raise error
            return {**chain, "pages": 1}

    monkeypatch.setattr(massive_rest, "MassiveREST", Fake)


def test_cli_defaults_to_massive_and_names_the_source(monkeypatch):
    chain = chain_from_snapshot([contract(700.0, "C", 1200, 0.18), contract(690.0, "P", 900, 0.21)])
    fake_client(monkeypatch, chain=chain)
    monkeypatch.setattr(cli_mod, "now_et", lambda: SESSION)
    result = gex("--ticker", "SPY")
    assert result.exit_code == 0, result.output
    assert "**Data:** Massive (delayed; options as of time unknown; spot from the options snapshot; 2 contracts" in (
        result.output
    )
    data = json.loads(gex("--ticker", "SPY", "--json").stdout)
    assert data["source"].startswith("Massive (delayed;")


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (MassiveError("no-key", "MASSIVE_API_KEY not set"), "set MASSIVE_API_KEY in .env"),
        (
            MassiveError(403, f"{TEST_BASE}/v3/snapshot/options/SPY"),
            "doesn't include options chain snapshots",
        ),
    ],
)
def test_cli_explains_massive_failures(monkeypatch, error, says):
    fake_client(monkeypatch, error=error)
    result = gex("--ticker", "SPY")
    assert result.exit_code == 1
    assert says in result.output


def test_cli_cboe_source_explains_the_block(monkeypatch):
    from tradekit.data import cboe

    def blocked(ticker):
        raise cboe.CBOEError("could not fetch CBOE chain for SPY: HTTP Error 403: Forbidden")

    monkeypatch.setattr(cboe, "fetch_cboe_chain", blocked)
    result = gex("--ticker", "SPY", "--source", "cboe")
    assert result.exit_code == 1
    assert "CBOE blocks automated access" in result.output and "--source massive" in result.output


# ----- the plan as seen live (2026-11-22): no underlying price or timeframe in the options snapshot -----


def live_shaped(strike, right, oi, iv, last_updated_ns=1795187102679000000):
    """What the user's plan returned: underlying_asset is {"ticker"} only, no last_quote."""
    item = contract(strike, right, oi, iv)
    item["underlying_asset"] = {"ticker": "SPY"}
    item["day"] = {"close": 5.0, "last_updated": last_updated_ns}
    return item


def test_without_an_underlying_price_spot_comes_from_the_stock_snapshot(monkeypatch):
    stock = {"status": "OK", "ticker": {"min": {"c": 697.49}, "day": {"c": 689.81}, "updated": 1795222800000000000}}
    calls = fake_requests(monkeypatch, [{"results": [live_shaped(700.0, "C", 1200, 0.18)]}, stock])
    chain = client().option_chain("SPY")
    assert chain["current_price"] == 697.49  # the latest minute, not the extended-hours day close
    assert chain["spot_source"] == "stock snapshot"
    assert chain["spot_asof"] == dt.datetime(2026, 11, 20, 20, 0, tzinfo=massive_rest.NY)
    assert chain["freshness"] == "unknown"
    assert chain["asof"] == dt.datetime.fromtimestamp(1795187102679000000 / 1e9, massive_rest.NY)
    assert calls[1][0] == f"{TEST_BASE}/v2/snapshot/locale/us/markets/stocks/tickers/SPY"


def test_index_without_an_underlying_price_is_refused_not_guessed(monkeypatch):
    item = live_shaped(6000.0, "C", 10, 0.2)
    item["underlying_asset"] = {"ticker": "I:SPX"}
    fake_requests(monkeypatch, [{"results": [item]}])
    with pytest.raises(MassiveError, match="index spot isn't supported yet"):
        client().option_chain("SPX")


def test_cli_data_line_says_what_the_plan_did_not_report(monkeypatch):
    chain = chain_from_snapshot([live_shaped(700.0, "C", 1200, 0.18), live_shaped(690.0, "P", 900, 0.21)])
    chain.update(
        current_price=697.49, spot_source="stock snapshot", spot_asof=dt.datetime(2026, 11, 20, 20, 0, tzinfo=ET)
    )
    fake_client(monkeypatch, chain=chain)
    monkeypatch.setattr(cli_mod, "now_et", lambda: SESSION)
    out = gex("--ticker", "SPY").output
    assert "delay not reported by the plan" in out
    assert "spot 697.49 from the stock snapshot, Fri 11-20 20:00 ET" in out


def test_client_reads_key_and_base_from_config(monkeypatch):
    from tradekit import config

    fake = SimpleNamespace(massive_api_key="from-config", massive_api_base="https://configured.test/")
    monkeypatch.setattr(config, "DataSettings", lambda: fake)
    c = MassiveREST(limiter=SimpleNamespace(acquire=lambda: None))
    assert (c.api_key, c.base_url) == ("from-config", "https://configured.test")
