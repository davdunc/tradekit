"""Depth source adapters (#9): pure mappings and failure modes. No broker SDKs or network needed."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from tradekit import cli as cli_mod
from tradekit.depth.events import FULL_DEPTH
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


def test_ibkr_without_ib_async_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "ib_async", None)  # make the import fail
    with pytest.raises(MissingExtra, match=r"tradekit-cli\[ibkr\]"):
        asyncio.run(IBKRSource().connect())


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


def test_cli_alpaca_warns_it_is_not_depth(monkeypatch, tmp_path):
    from tradekit.depth import recorder

    async def instant(self, stop):
        return None

    monkeypatch.setattr(recorder.Recorder, "run", instant)
    result = CliRunner().invoke(cli_mod.cli, ["depth", "record", "AAPL", "--source", "alpaca", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "not depth" in result.stdout
    assert "from alpaca into" in result.stdout
