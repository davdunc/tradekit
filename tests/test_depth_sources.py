"""Depth source adapters (#9): pure mappings and failure modes. No broker SDKs or network needed."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from tradekit import cli as cli_mod
from tradekit.depth.events import FULL_DEPTH
from tradekit.depth.recorder import PermanentlyUnavailable, Recorder
from tradekit.depth.sources import MissingExtra, factories
from tradekit.depth.sources.alpaca import AlpacaSource, book_updates_from_quote, trade_from_alpaca
from tradekit.depth.sources.das import DASSource
from tradekit.depth.sources.ibkr import IBKRSource, book_update_from_dom

# ----- IBKR: TWS updateMktDepthL2 side 0 = ask, 1 = bid; operation 0/1/2 = insert/update/delete -----


def _dom(**kw):
    base = {"position": 2, "marketMaker": "ARCA", "operation": 0, "side": 1, "price": 190.25, "size": 300}
    return SimpleNamespace(**(base | kw))


def test_ibkr_maps_side_op_and_level():
    u = book_update_from_dom("AAPL", _dom())
    assert (u.source, u.symbol, u.side, u.position, u.op) == ("ibkr", "AAPL", "bid", 2, "insert")
    assert (u.price, u.size, u.venue, u.depth_levels) == (190.25, 300.0, "ARCA", FULL_DEPTH)
    assert book_update_from_dom("AAPL", _dom(side=0, operation=1)).side == "ask"
    assert book_update_from_dom("AAPL", _dom(operation=1)).op == "update"


def test_ibkr_delete_carries_no_price_or_size():
    u = book_update_from_dom("AAPL", _dom(operation=2))
    assert (u.op, u.price, u.size) == ("delete", None, None)


def test_ibkr_malformed_tick_is_dropped():
    assert book_update_from_dom("AAPL", _dom(side=7)) is None
    assert book_update_from_dom("AAPL", _dom(operation=9)) is None


@pytest.mark.parametrize(
    ("code", "message"),
    [
        # Verbatim from a live paper account without depth subscriptions (2026-11-19):
        (2152, "Exchanges - Top: IBEOS; OVERNIGHT; Need additional market data permissions - Depth: BATS; NASDAQ"),
        (10089, "Requested market data requires additional subscription for API. AAPL NASDAQ.NMS/DEEP"),
        (310, "Can't find the subscribed market depth with tickerId:6"),
        (12345, "a code nobody has seen before"),  # unknown codes are recorded too
    ],
)
def test_ibkr_refusals_become_markers(code, message):
    from tradekit.depth.sources.ibkr import marker_from_tws_error

    m = marker_from_tws_error(6, code, message, SimpleNamespace(symbol="AAPL"))
    assert m is not None and m.marker == "error" and m.source == "ibkr"
    assert f"TWS {code}" in m.detail and "AAPL" in m.detail


@pytest.mark.parametrize("code", [2104, 2106, 2158])
def test_ibkr_data_farm_status_is_not_a_marker(code):
    from tradekit.depth.sources.ibkr import marker_from_tws_error

    assert marker_from_tws_error(-1, code, "Market data farm connection is OK:usfarm") is None


def test_ibkr_without_ib_async_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "ib_async", None)  # make the import fail
    with pytest.raises(MissingExtra, match=r"tradekit-cli\[ibkr\]"):
        asyncio.run(IBKRSource("127.0.0.1", 4002).connect())


# G12: host and port have no default, so a guessed port can't be refused and read as an outage.


@pytest.mark.parametrize(
    ("host", "port", "named"),
    [("", None, "IBKR_HOST and IBKR_PORT"), ("127.0.0.1", None, "IBKR_PORT"), ("", 4002, "IBKR_HOST")],
)
def test_ibkr_without_connection_settings_is_permanently_unavailable(host, port, named):
    with pytest.raises(PermanentlyUnavailable, match=named):
        asyncio.run(IBKRSource(host, port).connect())


def test_ibkr_missing_settings_are_checked_before_the_sdk(monkeypatch):
    """Unconfigured is reported as unconfigured, even where ib_async isn't installed."""
    monkeypatch.setitem(sys.modules, "ib_async", None)
    with pytest.raises(PermanentlyUnavailable, match="IBKR_PORT"):
        asyncio.run(IBKRSource("127.0.0.1", None).connect())


def test_settings_give_ibkr_no_default_host_or_port():
    from tradekit.config import DataSettings

    fields = DataSettings.model_fields
    assert fields["ibkr_host"].default == ""
    assert fields["ibkr_port"].default is None


def test_blank_ibkr_port_from_env_example_is_unset(monkeypatch):
    """.env.example ships ``IBKR_PORT=``; that must read as unset, not break every command."""
    from tradekit.config import DataSettings

    monkeypatch.setenv("IBKR_HOST", "")
    monkeypatch.setenv("IBKR_PORT", "")
    data = DataSettings()
    assert (data.ibkr_host, data.ibkr_port) == ("", None)


def test_ibkr_port_from_env_is_an_int(monkeypatch):
    from tradekit.config import DataSettings

    monkeypatch.setenv("IBKR_PORT", "4001")
    assert DataSettings().ibkr_port == 4001


def test_unconfigured_ibkr_in_auto_is_skipped_once_and_recorded_as_a_gap():
    sink = SimpleNamespace(events=[], flush=lambda: None)
    sink.add = sink.events.append
    recorder = Recorder([DASSource(), IBKRSource("", None)], ["SPY"], sink, retry_seconds=0.01)

    async def run_briefly():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, stop.set)
        await recorder.run(stop)

    asyncio.run(run_briefly())
    markers = [(e.marker, e.source, e.detail) for e in sink.events]
    ibkr_errors = [d for m, s, d in markers if m == "error" and s == "ibkr"]
    assert len(ibkr_errors) == 1, markers  # reported once, not on every retry
    assert "IBKR_HOST and IBKR_PORT" in ibkr_errors[0]
    assert [m for m, _, _ in markers].count("gap_start") == 1


# ----- Alpaca: level 1 only -----


def test_alpaca_quote_is_two_level_one_rows():
    q = SimpleNamespace(
        symbol="AAPL", bid_price=190.1, bid_size=2, ask_price=190.2, ask_size=3, bid_exchange="V", ask_exchange="Q"
    )
    rows = book_updates_from_quote(q)
    assert [(r.side, r.position, r.price, r.size, r.venue) for r in rows] == [
        ("bid", 0, 190.1, 2.0, "V"),
        ("ask", 0, 190.2, 3.0, "Q"),
    ]
    assert {r.depth_levels for r in rows} == {1}  # never mistakable for depth
    assert {r.source for r in rows} == {"alpaca"}


def test_alpaca_trade_mapping():
    t = trade_from_alpaca(SimpleNamespace(symbol="AAPL", price=190.15, size=100, exchange="V"))
    assert (t.source, t.symbol, t.price, t.size, t.venue) == ("alpaca", "AAPL", 190.15, 100.0, "V")


def test_alpaca_without_keys_is_unavailable():
    with pytest.raises(RuntimeError, match="APCA_API_KEY_ID"):
        asyncio.run(AlpacaSource("", "").connect())


def test_alpaca_rejects_unknown_feed():
    with pytest.raises(RuntimeError, match="iex"):
        asyncio.run(AlpacaSource("k", "s", feed="otc").connect())


def test_factories_read_settings(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "kid")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "sec")
    monkeypatch.setenv("ALPACA_FEED", "sip")
    monkeypatch.setenv("IBKR_PORT", "4001")
    from tradekit import config

    config.get_settings.cache_clear() if hasattr(config.get_settings, "cache_clear") else None
    made = factories(levels=5)
    alpaca, ibkr = made["alpaca"](), made["ibkr"]()
    assert (alpaca.key_id, alpaca.secret_key, alpaca.feed) == ("kid", "sec", "sip")
    assert (ibkr.port, ibkr.levels) == (4001, 5)


# ----- DAS: placeholder until finished on a DAS host -----


def test_das_is_not_implemented_yet():
    with pytest.raises(NotImplementedError, match="DAS Trader"):
        asyncio.run(DASSource().connect())


# ----- CLI -----


def test_cli_until_in_the_past_is_a_usage_error():
    result = CliRunner().invoke(cli_mod.cli, ["depth", "record", "AAPL", "--until", "00:00"])
    assert result.exit_code == 2
    assert "already passed" in result.output


def _settings(host="", port=None):
    data = SimpleNamespace(
        ibkr_host=host,
        ibkr_port=port,
        ibkr_client_id=17,
        apca_api_key_id="",
        apca_api_secret_key="",
        alpaca_feed="iex",
    )
    return SimpleNamespace(data=data)


def test_cli_ibkr_without_settings_is_a_usage_error(monkeypatch, tmp_path):
    from tradekit import config

    monkeypatch.setattr(config, "get_settings", lambda: _settings())
    result = CliRunner().invoke(cli_mod.cli, ["depth", "record", "SPY", "--source", "ibkr", "--out", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "IBKR_HOST and IBKR_PORT" in result.output


def test_cli_auto_without_ibkr_settings_warns_and_records(monkeypatch, tmp_path):
    from tradekit import config
    from tradekit.depth import recorder

    async def instant(self, stop):
        return None

    monkeypatch.setattr(config, "get_settings", lambda: _settings(host="127.0.0.1"))
    monkeypatch.setattr(recorder.Recorder, "run", instant)
    result = CliRunner().invoke(cli_mod.cli, ["depth", "record", "SPY", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "no IBKR fallback this run" in result.stderr
    assert "IBKR_PORT" in result.stderr


def test_cli_alpaca_warns_it_is_not_depth(monkeypatch, tmp_path):
    from tradekit.depth import recorder

    async def instant(self, stop):
        return None

    monkeypatch.setattr(recorder.Recorder, "run", instant)
    result = CliRunner().invoke(cli_mod.cli, ["depth", "record", "AAPL", "--source", "alpaca", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "not depth" in result.stdout
    assert "from alpaca into" in result.stdout
