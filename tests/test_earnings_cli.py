"""`tradekit earnings` (#27): event lookup, output, and the unavailable case. No network."""

import datetime as dt
import json
import math
import re

import pytest
from click.testing import CliRunner

from tradekit import cli as cli_mod
from tradekit.data import finviz_elite
from tradekit.data.finviz_elite import parse_earnings_date

D = dt.date

# ----- Finviz "Earnings Date" parsing (format verified against a live export, 2026-11-19) -----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10/29/2026 4:30:00 PM", (D(2026, 10, 29), "AMC")),
        ("11/19/2026 8:30:00 AM", (D(2026, 11, 19), "BMO")),
        ("11/19/2026 4:00:00 PM", (D(2026, 11, 19), "AMC")),  # at the close counts as after
        ("11/19/2026 12:00:00 PM", (D(2026, 11, 19), "unknown")),  # mid-session
        ("11/19/2026 9:30:00 AM", (D(2026, 11, 19), "unknown")),  # at the open is not before it
        ("11/19/2026", (D(2026, 11, 19), "unknown")),
    ],
)
def test_parse_earnings_date(raw, expected):
    assert parse_earnings_date(raw) == expected


@pytest.mark.parametrize("raw", [None, math.nan, "", "   ", "soon", "2026-11-19"])
def test_parse_earnings_date_absent_or_unreadable(raw):
    assert parse_earnings_date(raw) is None


# ----- the command -----


def _occ(expiry: dt.date, cp: str, strike: float) -> str:
    return f"AMZN{expiry:%y%m%d}{cp}{int(strike * 1000):08d}"


def _chain(straddles: dict, spot=254.0) -> dict:
    opts = []
    for expiry, straddle in straddles.items():
        for cp in "CP":
            opts.append({"option": _occ(expiry, cp, 255.0), "bid": straddle / 2 - 0.05, "ask": straddle / 2 + 0.05})
    return {"current_price": spot, "options": opts}


AMZN = _chain({D(2026, 10, 28): 3.95, D(2026, 10, 30): 19.23})


def _fake_event_chain(full: dict, choose_expiry, freshness: str = "live") -> dict:
    """What data.ibkr.fetch_event_chain returns: only the expiry the rule picks gets quoted."""
    from tradekit.analysis.gex import parse_occ_symbol

    expiries = {parse_occ_symbol(o["option"])[0] for o in full["options"]}
    chosen = choose_expiry(expiries)
    options = [o for o in full["options"] if parse_occ_symbol(o["option"])[0] == chosen]
    return {
        "current_price": full["current_price"],
        "options": options,
        "expirations": sorted(e.isoformat() for e in expiries),
        "freshness": freshness,
    }


@pytest.fixture
def chain(monkeypatch):
    """Serve a synthetic chain through the IB Gateway fetch (#55); returns a setter for others."""
    holder = {"data": AMZN, "freshness": "live", "chosen": []}

    def fake(ticker, choose_expiry, **kwargs):
        def record(expiries):
            holder["chosen"].append(choose_expiry(expiries))
            return holder["chosen"][-1]

        return _fake_event_chain(holder["data"], record, holder["freshness"])

    monkeypatch.setattr("tradekit.data.ibkr.fetch_event_chain", fake)
    setter = lambda data: holder.update(data=data)  # noqa: E731
    setter.holder = holder
    return setter


@pytest.fixture
def no_finviz(monkeypatch):
    monkeypatch.setattr(finviz_elite.FinvizEliteConfig, "from_env", classmethod(lambda cls: None))


def _plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def _run(*args):
    return CliRunner().invoke(cli_mod.cli, ["earnings", *args])


def test_given_date_and_timing_prices_the_event_expiry(chain, no_finviz):
    result = _run("amzn", "--date", "2026-10-29", "--timing", "amc")
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "AMZN earnings 2026-10-29 AMC" in out
    assert "event expiry 2026-10-30" in out
    assert "±7.57%" in out
    assert "1.55" not in out  # the 0DTE contract that held no event


def test_json_output(chain, no_finviz):
    result = _run("AMZN", "--date", "2026-10-29", "--timing", "AMC", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)  # the session banner goes to stderr; stdout is pure JSON
    assert data["event_expiry"] == "2026-10-30"
    assert data["expiry_quality"] == "event"
    assert data["implied_move_pct"] == pytest.approx(7.57, abs=0.01)
    assert data["source"] == "given"


def test_date_and_timing_looked_up_from_finviz(chain, monkeypatch):
    monkeypatch.setattr(
        finviz_elite.FinvizEliteConfig, "from_env", classmethod(lambda cls: finviz_elite.FinvizEliteConfig("tok"))
    )
    monkeypatch.setattr(
        finviz_elite.FinvizEliteProvider, "get_earnings_event", lambda self, t: (D(2026, 10, 29), "AMC")
    )
    result = _run("AMZN", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert (data["event_date"], data["timing"], data["source"]) == ("2026-10-29", "AMC", "Finviz Elite")
    assert data["event_expiry"] == "2026-10-30"


def test_no_event_expiry_is_reported_unavailable(chain, no_finviz):
    chain(_chain({D(2026, 10, 27): 3.0, D(2026, 10, 28): 3.95}))  # every expiry is before the event
    result = _run("AMZN", "--date", "2026-10-29", "--timing", "AMC")
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "UNAVAILABLE" in out and "never substituted" in out
    assert "%" not in out


def test_without_token_or_flags_asks_for_flags(chain, no_finviz):
    result = _run("AMZN")
    assert result.exit_code != 0
    assert "--date and --timing" in result.output


def test_bad_date_is_a_usage_error(chain, no_finviz):
    result = _run("AMZN", "--date", "10/29/2026")
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.output


# ----- #55: quotes come from IB Gateway, with their freshness -----


def test_only_the_event_expiry_is_quoted(chain, no_finviz):
    """The CLI passes the #27 event-expiry rule to the fetch, so the 0DTE contract is never requested."""
    result = _run("AMZN", "--date", "2026-10-29", "--timing", "AMC", "--json")
    assert result.exit_code == 0, result.output
    assert chain.holder["chosen"] == [D(2026, 10, 30)]


def test_json_names_the_quote_source(chain, no_finviz):
    data = json.loads(_run("AMZN", "--date", "2026-10-29", "--timing", "AMC", "--json").stdout)
    assert data["quotes"] == "IB Gateway (live)"


def test_frozen_quotes_are_labelled(chain, no_finviz):
    chain.holder["freshness"] = "frozen"
    out = _plain(_run("AMZN", "--date", "2026-10-29", "--timing", "AMC").output)
    assert "quotes from IB Gateway (frozen)" in out
    assert "last ones from the previous session" in out


def test_gateway_unavailable_is_a_clean_error(monkeypatch, no_finviz):
    from tradekit.data import ibkr

    def refuse(ticker, choose_expiry, **kwargs):
        raise ibkr.IBKRSnapshotError("set IBKR_HOST and IBKR_PORT for IB Gateway (4002 paper, 4001 live)")

    monkeypatch.setattr(ibkr, "fetch_event_chain", refuse)
    result = _run("AMZN", "--date", "2026-10-29", "--timing", "AMC")
    assert result.exit_code == 1
    assert "Option quotes unavailable: set IBKR_HOST and IBKR_PORT" in result.output
