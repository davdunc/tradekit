"""Tests for the Finviz Elite export column mapping.

The failure this guards against is silent rather than loud. Finviz's export takes numeric
column codes and returns *named* headers, and `get_quote()` reads the result back by header
name. A wrong code therefore does not raise and does not return a wrong number — it returns a
column the reader is not looking for, `row.get(...)` yields `None`, and a caller doing
`latest.get("rsi", 0)` turns absent data into a literal 0, which reads as maximally oversold.

`EXPECTED_HEADERS` below is a frozen snapshot of the live export, probed one block at a time on
2026-09-08 for codes 1-7, 40-54 and 55-72. Nothing here touches the network: the point is to
catch drift between our map and that verified reality, which is a code change, not a runtime
condition.
"""

from __future__ import annotations

import pytest

from tradekit.data.finviz_elite import FinvizEliteProvider

#: Finviz Elite export column code -> the header that code actually returns.
#: Verified against ``export.ashx?v=152&t=INTC&c=...`` on 2026-09-08.
EXPECTED_HEADERS: dict[int, str] = {
    1: "Ticker",
    2: "Company",
    3: "Sector",
    4: "Industry",
    5: "Country",
    6: "Market Cap",
    7: "P/E",
    42: "Performance (Week)",
    43: "Performance (Month)",
    44: "Performance (Quarter)",
    45: "Performance (Half Year)",
    46: "Performance (Year)",
    47: "Performance (YTD)",
    48: "Beta",
    49: "Average True Range",
    50: "Volatility (Week)",
    59: "Relative Strength Index (14)",
    60: "Change from Open",
    61: "Gap",
    62: "Analyst Recom",
    63: "Average Volume",
    64: "Relative Volume",
    65: "Price",
    66: "Change",
    67: "Volume",
    68: "Earnings Date",
    69: "Target Price",
    70: "IPO Date",
    71: "After-Hours Close",
    72: "After-Hours Change",
}

#: Our column key -> the header we expect the mapped code to return. This is the pairing that
#: actually broke: the code and the header-name lookup in `get_quote()` drifted apart.
KEY_TO_HEADER: dict[str, str] = {
    "ticker": "Ticker",
    "company": "Company",
    "sector": "Sector",
    "industry": "Industry",
    "country": "Country",
    "market_cap": "Market Cap",
    "pe": "P/E",
    "perf_w": "Performance (Week)",
    "perf_m": "Performance (Month)",
    "perf_q": "Performance (Quarter)",
    "perf_h": "Performance (Half Year)",
    "perf_y": "Performance (Year)",
    "perf_ytd": "Performance (YTD)",
    "beta": "Beta",
    "atr": "Average True Range",
    "vol_week": "Volatility (Week)",
    "rsi": "Relative Strength Index (14)",
    "change_open": "Change from Open",
    "gap": "Gap",
    "avg_vol": "Average Volume",
    "rvol": "Relative Volume",
    "price": "Price",
    "change": "Change",
    "volume": "Volume",
    "earnings": "Earnings Date",
    "target": "Target Price",
    "ipo": "IPO Date",
    "ah_close": "After-Hours Close",
    "ah_change": "After-Hours Change",
}


@pytest.mark.parametrize("key,header", sorted(KEY_TO_HEADER.items()))
def test_each_column_code_returns_the_header_we_read_back(key, header):
    """Every mapped key points at the code whose header its consumer looks for."""
    code = FinvizEliteProvider.SCREENER_COLS[key]
    assert code in EXPECTED_HEADERS, f"{key!r} maps to code {code}, which is not in the verified table"
    assert EXPECTED_HEADERS[code] == header, (
        f"{key!r} maps to code {code}, which returns {EXPECTED_HEADERS[code]!r}, "
        f"but consumers read {header!r}. This is the silent-None failure from issue #13."
    )


def test_every_screener_col_is_covered_by_this_test():
    """A new column must arrive with a verified header, or this test is decorative."""
    unmapped = set(FinvizEliteProvider.SCREENER_COLS) - set(KEY_TO_HEADER)
    assert not unmapped, (
        f"{sorted(unmapped)} added to SCREENER_COLS without a verified header. "
        "Probe it against the live export and add it to KEY_TO_HEADER."
    )


def test_the_three_codes_from_issue_13():
    """The regression itself, named. Each of these was one too high."""
    assert FinvizEliteProvider.SCREENER_COLS["rsi"] == 59
    assert FinvizEliteProvider.SCREENER_COLS["change_open"] == 60
    assert FinvizEliteProvider.SCREENER_COLS["gap"] == 61


def test_rsi_is_in_the_default_quote_columns():
    """`rsi` was on the common path, which is why the bug mattered rather than lurked."""
    import inspect

    source = inspect.getsource(FinvizEliteProvider.get_quotes)
    assert '"rsi"' in source


def test_analyst_recom_gap_is_intentional():
    """62 is Analyst Recom and is deliberately unmapped.

    The map jumping 61 -> 63 looks like an error and invites someone to close it by shifting
    the neighbours, which is precisely how issue #13 would be reintroduced.
    """
    assert 62 not in FinvizEliteProvider.SCREENER_COLS.values()
    assert EXPECTED_HEADERS[62] == "Analyst Recom"


def test_no_two_keys_share_a_code():
    codes = list(FinvizEliteProvider.SCREENER_COLS.values())
    duplicates = {c for c in codes if codes.count(c) > 1}
    assert not duplicates, f"codes {sorted(duplicates)} are mapped by more than one key"


def test_after_hours_and_earnings_block_is_correct():
    """Issue #13 flagged 68-71 as possibly sharing the off-by-one. Probing says they do not.

    The empty single-column export that raised the suspicion was a quirk of the probe, not a
    wrong code — worth asserting so the question is not reopened from memory.
    """
    for key, code in (("earnings", 68), ("target", 69), ("ipo", 70), ("ah_close", 71), ("ah_change", 72)):
        assert FinvizEliteProvider.SCREENER_COLS[key] == code
