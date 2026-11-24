"""DAS depth source (#9), tested on the real probe captures from #9.

Fixtures are verbatim from the issue (credentials masked by the probe or by hand):
- ``overnight-20261123T030035Z.jsonl``: 67 lines, BOAT/MOON overnight venues, only REMOVED.
- ``premarket-20261123T091113Z-excerpt.jsonl``: 62 lines at 04:11 ET, UPDATED/REMOVED, $T&S.
"""

import asyncio
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradekit.depth.events import VENUE_KEYED, BookUpdate, Marker, Trade
from tradekit.depth.recorder import PermanentlyUnavailable
from tradekit.depth.sources.das import DASSource, LineParser, trade_time

FIXTURES = Path(__file__).parent / "fixtures" / "das"


def replay(name):
    """Parse a capture's received lines in order, timed by their receive time."""
    parser, events = LineParser(), []
    for raw in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        row = json.loads(raw)
        if row["dir"] == "recv":
            events += parser.parse(row["line"], dt.datetime.fromisoformat(row["t"]))
    return events


def books(events):
    return [e for e in events if isinstance(e, BookUpdate)]


# ----- the captures -----


def test_overnight_capture_is_all_venue_deletes():
    events = replay("overnight-20261123T030035Z.jsonl")
    assert not [e for e in events if isinstance(e, Marker)]  # every line understood
    book = books(events)
    assert len(book) == 47 and {e.op for e in book} == {"delete"}
    assert {e.venue for e in book} == {"BOAT", "MOON"}
    assert all(e.position == VENUE_KEYED and e.source == "das" for e in book)
    first = book[0]
    assert (first.symbol, first.side, first.price, first.size) == ("DELL", "ask", 293.4, 2)
    assert first.ts == dt.datetime(2026, 11, 23, 3, 0, 41, 956723, tzinfo=dt.UTC)  # receipt, not the stamp


def test_premarket_capture_first_quote_per_venue_is_an_insert():
    events = replay("premarket-20261123T091113Z-excerpt.jsonl")
    assert not [e for e in events if isinstance(e, Marker)]
    dell = [e for e in books(events) if e.symbol == "DELL"]
    inserts = [e for e in dell if e.op == "insert"]
    assert {(e.side, e.venue) for e in inserts} == {("bid", v) for v in ("BATS", "NASD", "EDGX", "ARCA", "MEMX")} | {
        ("ask", v) for v in ("NASD", "MEMX", "ARCA", "EDGX", "BATS", "24X")
    }
    updates = [e for e in dell if e.op == "update"]
    assert [(e.venue, e.price) for e in updates] == [
        ("EDGX", 301.84),
        ("EDGX", 301.98),
        ("EDGX", 302),
        ("EDGX", 302.12),
    ]


def test_premarket_removal_with_zero_price_and_size_is_a_delete_with_nulls():
    removals = [e for e in books(replay("premarket-20261123T091113Z-excerpt.jsonl")) if e.op == "delete"]
    assert [(e.symbol, e.side, e.venue, e.price, e.size) for e in removals] == [("BE", "bid", "24X", None, None)] * 2


def test_time_and_sales_is_timed_by_its_et_stamp_not_the_replay_burst():
    trades = [e for e in replay("premarket-20261123T091113Z-excerpt.jsonl") if isinstance(e, Trade)]
    assert len(trades) == 15
    first = trades[0]
    assert (first.symbol, first.price, first.size, first.venue) == ("BE", 153.97, 5, "ARCA")
    assert first.ts == dt.datetime(2026, 11, 23, 9, 10, 44, tzinfo=dt.UTC)  # 04:10:44 ET, received 04:11:23
    assert {t.venue for t in trades} >= {"ARCA", "NASD", "FADF", "EDGX"}


def test_quotes_and_banners_are_not_depth():
    parser = LineParser()
    for line in ["#LOGIN SUCCESSED", "$Quote DELL V:70975 tradesAllDay:2266", "#IPOS account symb"]:
        assert parser.parse(line) == []


# ----- what isn't known is reported, not guessed -----


def test_unknown_action_is_one_error_marker_not_a_guess():
    parser = LineParser()
    first = parser.parse("$Lv2 DELL B EDGX 302 1 ADDED 03:11:19")
    again = parser.parse("$Lv2 DELL B EDGX 302 1 ADDED 03:11:19")
    assert [type(e) for e in first] == [Marker] and "ADDED" in first[0].detail and again == []


@pytest.mark.parametrize(
    "line",
    [
        "$Lv2 DELL B EDGX 302 UPDATED 03:11:19",
        "$Lv2 DELL X EDGX 302 1 UPDATED 03:11:19",
        "$Lv2 DELL B EDGX p 1 UPDATED t",
    ],
)
def test_malformed_lv2_lines_are_markers(line):
    assert [type(e) for e in LineParser().parse(line)] == [Marker]


def test_a_removed_venue_that_returns_is_an_insert_again():
    parser = LineParser()
    ops = [
        parser.parse(f"$Lv2 BE B 24X {p} {s} {a} 03:14:00")[0].op
        for p, s, a in [(154, 1, "UPDATED"), (154.1, 2, "UPDATED"), (0, 0, "REMOVED"), (154, 1, "UPDATED")]
    ]
    assert ops == ["insert", "update", "delete", "insert"]


def test_trade_stamp_after_receipt_belongs_to_the_previous_day():
    received = dt.datetime(2026, 11, 24, 5, 0, 5, tzinfo=dt.UTC)  # 00:00:05 ET
    assert trade_time(dt.time(23, 59, 58), received) == dt.datetime(2026, 11, 24, 4, 59, 58, tzinfo=dt.UTC)
    assert trade_time(dt.time(0, 0, 1), received) == dt.datetime(2026, 11, 24, 5, 0, 1, tzinfo=dt.UTC)


# ----- the source against a fake DAS replaying the capture's login -----

BANNER = [
    r["line"]
    for r in map(
        json.loads, (FIXTURES / "premarket-20261123T091113Z-excerpt.jsonl").read_text(encoding="utf-8").splitlines()
    )
    if r["dir"] == "recv" and r["line"].startswith("#")
]


class FakeDAS:
    def __init__(self, banner=BANNER, data=()):
        self.banner, self.data, self.received = banner, list(data), []

    async def handle(self, reader, writer):
        while raw := await reader.readline():
            line = raw.decode().rstrip("\r\n")
            self.received.append(line)
            if line.startswith("LOGIN"):
                writer.write("".join(f"{b}\r\n" for b in self.banner).encode())
            elif line.startswith("SB ") and line.endswith("tms") and line.split()[1] == "DELL":
                writer.write("".join(f"{d}\r\n" for d in self.data).encode())
                await writer.drain()
                writer.close()  # DAS going away: events() must end
                return
            await writer.drain()
        writer.close()


def with_fake(fake, body):
    async def main():
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        src = DASSource("127.0.0.1", port, "me", "s3cret", "TR1", login_wait=1.0)
        try:
            return await body(src)
        finally:
            await src.close()
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def test_source_logs_in_watch_subscribes_depth_only_and_ends_on_close():
    data = ["$Lv2 DELL B EDGX 302 1 UPDATED 03:11:19", "$T&S DELL 302 5 F 04:10:44 EDGX I 64"]
    fake = FakeDAS(data=data)

    async def body(src):
        await src.connect()
        await src.subscribe(["DELL"])
        return [e async for e in src.events()]

    events = with_fake(fake, body)
    assert fake.received == ["LOGIN me s3cret TR1 1", "SB DELL Lv2", "SB DELL tms"]
    assert [type(e) for e in events] == [BookUpdate, Trade]
    assert events[0].op == "insert" and events[0].venue == "EDGX"


def test_source_refuses_a_connection_without_the_watch_header():
    fake = FakeDAS(banner=["#Welcome to DAS Command API", "#LOGIN SUCCESSED", "#POS account symb"])
    with pytest.raises(PermanentlyUnavailable, match="WATCH mode not confirmed"):
        with_fake(fake, lambda src: src.connect())


def test_source_refuses_a_failed_login():
    fake = FakeDAS(banner=["#LOGIN FAILED invalid password"])
    with pytest.raises(PermanentlyUnavailable, match="login refused"):
        with_fake(fake, lambda src: src.connect())


def test_unconfigured_source_names_every_setting():
    with pytest.raises(PermanentlyUnavailable) as err:
        asyncio.run(DASSource("", None, "", "", "").connect())
    for name in ("DAS_HOST", "DAS_PORT", "DAS_USER", "DAS_PASSWORD", "DAS_ACCOUNT"):
        assert name in str(err.value)


def test_factory_passes_the_das_settings(monkeypatch):
    from tradekit import config
    from tradekit.depth.sources import factories

    data = SimpleNamespace(
        das_host="127.0.0.1",
        das_port=9910,
        das_user="u",
        das_password="p",
        das_account="a",
        ibkr_host="",
        ibkr_port=None,
        ibkr_client_id=17,
        apca_api_key_id="",
        apca_api_secret_key="",
        alpaca_feed="iex",
    )
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data=data))
    src = factories()["das"]()
    assert (src.host, src.port, src.user, src.account) == ("127.0.0.1", 9910, "u", "a")


def test_cli_das_only_without_settings_is_a_usage_error(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from tradekit import cli as cli_mod
    from tradekit import config

    data = SimpleNamespace(
        das_host="",
        das_port=None,
        das_user="",
        das_password="",
        das_account="",
        ibkr_host="",
        ibkr_port=None,
        ibkr_client_id=17,
        ibkr_depth_limit=4,
        apca_api_key_id="",
        apca_api_secret_key="",
        alpaca_feed="iex",
    )
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data=data))
    result = CliRunner().invoke(cli_mod.cli, ["depth", "record", "AAPL", "--source", "das", "--out", str(tmp_path)])
    assert result.exit_code == 2 and "DAS_HOST" in result.output and "127.0.0.1" in result.output
