"""#17: a scanner whose candidates all fail enrichment is a dead tool, not a quiet tape."""

import pandas as pd
import pytest
from click.testing import CliRunner

from tradekit.errors import EnrichmentError
from tradekit.screener import premarket

DOUBLED = ["FFGL", "OOCC", "SSSL", "CCHYM"]  # #16's corrupted symbols: none of them exist


class FakeProvider:
    """``quotes[ticker]`` is a quote dict, or an exception to raise; premarket rows are fixed."""

    def __init__(self, quotes: dict | None = None, premarket_rows: list | None = None):
        self.quotes = quotes or {}
        self.premarket_rows = premarket_rows or []
        self.quote_calls: list[str] = []

    def get_quote(self, ticker):
        self.quote_calls.append(ticker)
        q = self.quotes.get(ticker, KeyError(f"Quote not found for symbol: {ticker}"))
        if isinstance(q, Exception):
            raise q
        return q

    def get_premarket(self, ticker):
        return {"pre_price": 0, "gap_pct": 0, "has_premarket": False}

    def get_multiple_premarket(self, tickers):
        return list(self.premarket_rows)


def _seed(monkeypatch, tickers):
    monkeypatch.setattr(premarket, "finviz_gainers", lambda **kw: pd.DataFrame({"Ticker": tickers}))


def _quote(prev_close=10.0, price=10.0, volume=1_000_000, avg_volume=1_000_000):
    return {"price": price, "prev_close": prev_close, "volume": volume, "avg_volume": avg_volume, "name": "Co"}


# ----- scan_premarket (scan, morning, report, gameplan) -----


def test_scan_raises_when_no_candidate_resolves(monkeypatch):
    _seed(monkeypatch, DOUBLED)
    provider = FakeProvider()  # every quote raises "not found"
    with pytest.raises(EnrichmentError) as exc:
        premarket.scan_premarket(provider=provider)
    assert exc.value.attempted == DOUBLED
    assert "FFGL" in str(exc.value) and "not an empty market" in str(exc.value)
    assert provider.quote_calls == DOUBLED[: premarket.ENRICHMENT_PROBE]  # bounded re-check


def test_scan_raises_when_quotes_have_no_previous_close(monkeypatch):
    _seed(monkeypatch, ["FFGL", "OOCC"])
    provider = FakeProvider(quotes={"FFGL": {"prev_close": 0}, "OOCC": {}})
    with pytest.raises(EnrichmentError):
        premarket.scan_premarket(provider=provider)


def test_scan_real_symbols_without_premarket_activity_is_empty(monkeypatch):
    """Early in the session real symbols have no pre-market trades yet: empty, not an error."""
    _seed(monkeypatch, ["AAPL", "MSFT"])
    provider = FakeProvider(quotes={"AAPL": _quote(prev_close=190.0)})
    assert premarket.scan_premarket(provider=provider).empty


def test_scan_with_no_candidates_is_empty(monkeypatch):
    _seed(monkeypatch, [])
    provider = FakeProvider()
    assert premarket.scan_premarket(provider=provider).empty
    assert provider.quote_calls == []


# ----- scan_previous_movers (second-day, gameplan) -----


def test_second_day_raises_when_no_candidate_resolves(monkeypatch):
    tickers = [f"X{i}" for i in range(25)]
    _seed(monkeypatch, tickers)
    with pytest.raises(EnrichmentError) as exc:
        premarket.scan_previous_movers(provider=FakeProvider())
    assert exc.value.attempted == tickers[:20]  # the 20 actually tried


def test_second_day_resolved_but_filtered_out_is_empty(monkeypatch):
    """Quotes resolve, moves are just too small: a genuine empty result (thresholds do matter)."""
    _seed(monkeypatch, ["AAPL"])
    provider = FakeProvider(quotes={"AAPL": _quote(prev_close=100.0, price=101.0)})
    assert premarket.scan_previous_movers(provider=provider, min_change_pct=5.0).empty


# ----- CLI -----


def test_cli_second_day_reports_unavailable_without_threshold_hint(monkeypatch):
    from tradekit import cli as cli_mod

    def boom(**kwargs):
        raise EnrichmentError(DOUBLED)

    monkeypatch.setattr(premarket, "scan_previous_movers", boom)
    result = CliRunner().invoke(cli_mod.cli, ["second-day"])
    assert result.exit_code == 3
    assert "UNAVAILABLE" in result.output
    assert "FFGL" in result.output
    assert "Try lowering" not in result.output


def test_cli_scan_reports_unavailable(monkeypatch):
    from tradekit import cli as cli_mod

    def boom(**kwargs):
        raise EnrichmentError(DOUBLED)

    monkeypatch.setattr(premarket, "scan_premarket", boom)
    result = CliRunner().invoke(cli_mod.cli, ["scan"])
    assert result.exit_code == 3
    assert "UNAVAILABLE" in result.output
