"""data.ibkr.fetch_event_chain: the earnings straddle's option quotes from IB Gateway (#55).

Run against a fake ib_async, so no Gateway is needed. The shape it returns is what
analysis.earnings.implied_move takes, so the last test runs one into the other.
"""

import asyncio
import datetime as dt
import sys
from types import SimpleNamespace

import pytest

from tradekit.analysis.earnings import implied_move, select_event_expiry

NAN = float("nan")
EXPIRIES = ["20261204", "20261211", "20261218"]
STRIKES = [330.0, 335.0, 340.0, 345.0, 350.0, 355.0, 360.0]


class Event(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


def fake_ib_async(monkeypatch, *, spot=344.87, quotes=None, missing_strikes=(), md_type=2, stock_md_type=None):
    """A fake ib_async. ``quotes`` maps (strike, right) to (bid, ask, last); others get -1/-1/nan."""
    quotes = quotes or {}
    requested = []

    class Stock:
        def __init__(self, symbol, *a):
            self.symbol, self.conId, self.localSymbol = symbol, 0, symbol

    class Option:
        def __init__(self, symbol, expiry, strike, right, exchange, tradingClass=""):
            self.symbol, self.expiry, self.strike, self.right = symbol, expiry, strike, right
            self.conId = 0
            self.localSymbol = f"{symbol:<6}{expiry[2:]}{right}{int(round(strike * 1000)):08d}"

    class IB:
        def __init__(self):
            self.errorEvent = Event()
            self.market_data_type = None

        async def connectAsync(self, *a, **k):
            return None

        async def qualifyContractsAsync(self, *contracts):
            for c in contracts:
                if isinstance(c, Option) and c.strike in missing_strikes:
                    continue
                c.conId = 1
            return [c if c.conId else None for c in contracts]

        def reqMarketDataType(self, t):
            self.market_data_type = t

        def reqMktData(self, contract, *a):
            requested.append(contract)
            if isinstance(contract, Stock):
                stock_type = md_type if stock_md_type is None else stock_md_type
                t = SimpleNamespace(marketDataType=stock_type, close=spot, bid=NAN, ask=NAN, last=NAN)
                t.marketPrice = lambda: spot if spot else NAN
                return t
            bid, ask, last = quotes.get((contract.strike, contract.right), (-1.0, -1.0, NAN))
            return SimpleNamespace(marketDataType=md_type, bid=bid, ask=ask, last=last)

        def cancelMktData(self, contract):
            pass

        async def reqSecDefOptParamsAsync(self, *a):
            return [SimpleNamespace(exchange="SMART", tradingClass="AAPL", expirations=EXPIRIES, strikes=STRIKES)]

        def disconnect(self):
            pass

    monkeypatch.setitem(sys.modules, "ib_async", SimpleNamespace(IB=IB, Stock=Stock, Option=Option))
    return requested


def run(choose_expiry, strikes=3, wait=0.3, grace=0.05):
    from tradekit.data.ibkr import _event_chain

    return asyncio.run(_event_chain("AAPL", choose_expiry, "127.0.0.1", 4002, 18, strikes, wait, grace))


def first(expiries):
    return min(expiries)


def test_quotes_the_strikes_nearest_spot_on_the_chosen_expiry_only(monkeypatch):
    requested = fake_ib_async(monkeypatch)
    chain = run(first, strikes=3)
    options = [o for o in requested if hasattr(o, "right")]
    assert {o.expiry for o in options} == {"20261204"}
    assert sorted({o.strike for o in options}) == [340.0, 345.0, 350.0]  # nearest 344.87
    assert len(chain["options"]) == 6  # call and put at each
    assert chain["current_price"] == 344.87
    assert chain["expirations"] == ["2026-12-04", "2026-12-11", "2026-12-18"]


def test_choose_expiry_sees_every_listed_expiry(monkeypatch):
    fake_ib_async(monkeypatch)
    seen = []
    run(lambda e: seen.append(e) or None)
    assert seen == [{dt.date(2026, 12, 4), dt.date(2026, 12, 11), dt.date(2026, 12, 18)}]


def test_no_qualifying_expiry_quotes_nothing(monkeypatch):
    requested = fake_ib_async(monkeypatch)
    chain = run(lambda e: None)
    assert chain["options"] == []
    assert not [o for o in requested if hasattr(o, "right")]


def test_ibkr_no_quote_minus_one_becomes_none(monkeypatch):
    fake_ib_async(monkeypatch, quotes={(345.0, "C"): (5.95, 10.15, 8.35)})
    chain = run(first, strikes=1)
    by_right = {o["option"][-9]: o for o in chain["options"]}
    assert (by_right["C"]["bid"], by_right["C"]["ask"], by_right["C"]["last_trade_price"]) == (5.95, 10.15, 8.35)
    assert (by_right["P"]["bid"], by_right["P"]["ask"], by_right["P"]["last_trade_price"]) == (None, None, None)


def test_a_strike_not_listed_for_the_expiry_is_skipped(monkeypatch):
    fake_ib_async(monkeypatch, missing_strikes=(345.0,))
    chain = run(first, strikes=3)
    assert all("00345000" not in o["option"] for o in chain["options"])
    assert len(chain["options"]) == 4


def test_freshness_is_reported(monkeypatch):
    fake_ib_async(monkeypatch, md_type=2)
    assert run(first)["freshness"] == "frozen"


def test_freshness_is_the_stalest_data_used(monkeypatch):
    """Seen live on a Saturday: the stock came back live (type 1) while the options were frozen."""
    fake_ib_async(monkeypatch, md_type=2, stock_md_type=1)
    assert run(first)["freshness"] == "frozen"


def test_all_live_is_live(monkeypatch):
    fake_ib_async(monkeypatch, md_type=1)
    assert run(first)["freshness"] == "live"


def test_no_underlying_price_is_an_error(monkeypatch):
    from tradekit.data.ibkr import IBKRSnapshotError

    fake_ib_async(monkeypatch, spot=None)
    with pytest.raises(IBKRSnapshotError, match="no price"):
        run(first)


def test_missing_gateway_settings_say_what_to_set(monkeypatch):
    from tradekit import config
    from tradekit.data import ibkr

    data = SimpleNamespace(ibkr_host="", ibkr_port=None, ibkr_client_id=17)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data=data))
    with pytest.raises(ibkr.IBKRSnapshotError, match="IBKR_HOST and IBKR_PORT"):
        ibkr.fetch_event_chain("AAPL", first)


def test_the_chain_prices_an_event_straddle(monkeypatch):
    """End to end on the fake: the #27 rule picks the expiry, implied_move prices the ATM straddle."""
    fake_ib_async(monkeypatch, quotes={(345.0, "C"): (8.0, 8.2, NAN), (345.0, "P"): (7.0, 7.2, NAN)})
    event = dt.date(2026, 12, 3)
    chain = run(lambda e: select_event_expiry(e, event, "AMC").expiry, strikes=1)
    result = implied_move(chain, ticker="AAPL", event_date=event, timing="AMC")
    assert result["event_expiry"] == "2026-12-04"
    assert result["straddle"] == pytest.approx(15.2)
    assert result["implied_move_pct"] == pytest.approx(15.2 / 344.87 * 100, abs=0.01)


# ----- #56 review: don't wait out the deadline when there is nothing more to get -----


def timed(choose_expiry, **kwargs):
    import time

    started = time.monotonic()
    chain = run(choose_expiry, **kwargs)
    return chain, time.monotonic() - started


def test_last_trade_only_after_hours_returns_after_the_grace_period(monkeypatch):
    last_only = {(345.0, r): (-1.0, -1.0, 8.35) for r in "CP"}
    fake_ib_async(monkeypatch, quotes=last_only)
    chain, took = timed(first, strikes=1, wait=8.0, grace=0.2)
    assert took < 2.0, f"waited {took:.1f}s although every leg had a last trade"
    assert all(o["last_trade_price"] == 8.35 for o in chain["options"])


def test_full_quotes_return_at_once(monkeypatch):
    both = {(345.0, r): (8.0, 8.2, 8.1) for r in "CP"}
    fake_ib_async(monkeypatch, quotes=both)
    _, took = timed(first, strikes=1, wait=8.0, grace=5.0)
    assert took < 1.0  # well under the 5 s grace: nothing was missing


def test_nothing_to_quote_returns_at_once(monkeypatch):
    fake_ib_async(monkeypatch, missing_strikes=tuple(STRIKES))  # no contract qualifies
    chain, took = timed(first, strikes=3, wait=8.0, grace=5.0)
    assert chain["options"] == [] and took < 1.0


def test_unpriced_legs_still_wait_to_the_deadline(monkeypatch):
    """No bid, ask or last at all: keep waiting (up to `wait`) rather than give up early."""
    fake_ib_async(monkeypatch)  # every leg -1/-1/nan
    _, took = timed(first, strikes=1, wait=0.5, grace=0.05)
    assert took >= 0.5
