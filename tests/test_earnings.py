"""Earnings implied move: the straddle must be priced on an expiry that contains the event (#27)."""

import datetime as dt

import pytest

from tradekit.analysis.earnings import atm_straddle, implied_move, select_event_expiry

D = dt.date


def _occ(root: str, expiry: dt.date, cp: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{cp}{int(strike * 1000):08d}"


def _chain(spot: float, straddles: dict[dt.date, float], strikes=(250.0, 255.0, 260.0)) -> dict:
    """Synthetic CBOE payload: ``straddles[expiry]`` is the ATM straddle (split evenly)."""
    atm = min(strikes, key=lambda k: abs(k - spot))
    options = []
    for expiry, straddle in straddles.items():
        for k in strikes:
            leg = straddle / 2 if k == atm else 0.5  # far legs priced low, never ATM
            for cp in "CP":
                options.append({"option": _occ("AMZN", expiry, cp, k), "bid": leg - 0.05, "ask": leg + 0.05})
    return {"current_price": spot, "options": options}


# The #27 reproduction: AMZN reports Thu 2026-10-29 after the close.
AMZN = _chain(254.0, {D(2026, 10, 28): 3.95, D(2026, 10, 30): 19.23, D(2026, 11, 2): 20.05})


def test_amc_skips_the_0dte_expiry_and_prices_friday():
    r = implied_move("AMZN", D(2026, 10, 29), "AMC", fetch=lambda t: AMZN)
    assert r["event_expiry"] == "2026-10-30"
    assert r["expiry_quality"] == "event"
    assert r["implied_move_pct"] == pytest.approx(7.57, abs=0.01)
    # The bug: the first expiry on or after today (10-28) gave 1.55%.
    assert atm_straddle(AMZN["options"], D(2026, 10, 28), 254.0)[1] / 254.0 * 100 == pytest.approx(1.55, abs=0.01)


def test_amc_event_on_thursday_selects_friday_over_0dte():
    choice = select_event_expiry([D(2026, 10, 29), D(2026, 10, 30)], D(2026, 10, 29), "AMC")
    assert (choice.expiry, choice.quality, choice.days_after_event) == (D(2026, 10, 30), "event", 1)


def test_bmo_event_on_day_d_selects_the_d_expiry():
    choice = select_event_expiry([D(2026, 10, 29), D(2026, 10, 30)], D(2026, 10, 29), "BMO")
    assert (choice.expiry, choice.quality, choice.days_after_event) == (D(2026, 10, 29), "event", 0)


def test_unknown_timing_is_treated_as_amc():
    choice = select_event_expiry([D(2026, 10, 29), D(2026, 10, 30)], D(2026, 10, 29), "unknown")
    assert choice.expiry == D(2026, 10, 30)


def test_only_expiries_before_the_event_is_unusable():
    r = implied_move(
        "AMZN", D(2026, 10, 29), "AMC", fetch=lambda t: _chain(254.0, {D(2026, 10, 27): 3.0, D(2026, 10, 28): 3.95})
    )
    assert r["event_expiry"] is None
    assert r["expiry_quality"] == "unusable"
    assert r["implied_move_pct"] is None


def test_far_first_qualifying_expiry_is_diluted():
    choice = select_event_expiry([D(2026, 10, 28), D(2026, 11, 20)], D(2026, 10, 29), "AMC")
    assert (choice.expiry, choice.quality, choice.days_after_event) == (D(2026, 11, 20), "diluted", 22)


def test_beyond_max_days_is_unusable():
    choice = select_event_expiry([D(2026, 12, 18)], D(2026, 10, 29), "AMC")
    assert (choice.expiry, choice.quality) == (None, "unusable")


def test_unpriced_atm_straddle_is_unusable():
    chain = {
        "current_price": 254.0,
        "options": [{"option": _occ("AMZN", D(2026, 10, 30), "C", 255.0), "bid": 9.5, "ask": 9.7}],  # no put leg
    }
    r = implied_move("AMZN", D(2026, 10, 29), "AMC", fetch=lambda t: chain)
    assert r["event_expiry"] == "2026-10-30"
    assert (r["expiry_quality"], r["implied_move_pct"]) == ("unusable", None)


def test_straddle_uses_last_trade_when_unquoted():
    exp = D(2026, 10, 30)
    options = [
        {"option": _occ("AMZN", exp, "C", 255.0), "bid": 0, "ask": 0, "last_trade_price": 9.0},
        {"option": _occ("AMZN", exp, "P", 255.0), "bid": 9.9, "ask": 10.1},
    ]
    assert atm_straddle(options, exp, 254.0) == (255.0, pytest.approx(19.0))
