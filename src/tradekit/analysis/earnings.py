"""Earnings implied move from the option chain, priced on the expiry that contains the event.

The implied move is the at-the-money straddle divided by spot, and it only measures the
earnings event if the straddle's expiry comes **after** the event. The first prototype
(``earnings_moves.py``) priced the first expiry on or after today. For a name reporting later in
the week that is an earlier contract with no event in it, and the move came out several times
too small (#27: AMZN reporting Thu 2026-10-29 after the close showed 1.55% from the 10-28 0DTE
straddle instead of 7.57% from 10-30). The reaction ratio, ``|gap| / implied``, then inflates by
the same factor, and an ordinary reaction gets labelled an OVERREACTION.

Expiry rules (design spec §6, expiry hygiene):

- **BMO** on day D: the event happens before D's close, so the D expiry contains it.
- **AMC** on day D: the event happens after D's close, so the first expiry *later than* D.
- **Unknown** timing is treated as AMC: the later expiry always contains the event.

``expiry_quality`` says how much of the straddle is the event:

- ``event``: the chosen expiry is within ``event_window_days`` of the event, so the
  straddle is mostly the earnings move.
- ``diluted``: the first qualifying expiry is further out (for example only monthlies are
  listed), so it also prices ordinary non-event days and overstates the event move somewhat.
- ``unusable``: no expiry after the event within ``max_days``. Never fall back to a
  non-event expiry: no number is better than a wrong one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from tradekit.analysis.gex import parse_occ_symbol

Timing = Literal["BMO", "AMC", "unknown"]

#: A qualifying expiry this close to the event is priced mostly by the event.
EVENT_WINDOW_DAYS = 7
#: Beyond this, a straddle says little about one earnings release.
MAX_DAYS = 30


@dataclass(frozen=True)
class ExpiryChoice:
    """The expiry an event straddle should be priced on, and how clean it is."""

    expiry: dt.date | None
    quality: Literal["event", "diluted", "unusable"]
    days_after_event: int | None


def select_event_expiry(
    expiries: Iterable[dt.date],
    event_date: dt.date,
    timing: Timing,
    *,
    event_window_days: int = EVENT_WINDOW_DAYS,
    max_days: int = MAX_DAYS,
) -> ExpiryChoice:
    """The first expiry whose expiration falls after the earnings event (#27)."""
    if timing == "BMO":
        qualifies = [e for e in set(expiries) if e >= event_date]
    else:  # AMC, and unknown treated as AMC
        qualifies = [e for e in set(expiries) if e > event_date]
    qualifies = sorted(e for e in qualifies if (e - event_date).days <= max_days)
    if not qualifies:
        return ExpiryChoice(None, "unusable", None)
    expiry = qualifies[0]
    days = (expiry - event_date).days
    return ExpiryChoice(expiry, "event" if days <= event_window_days else "diluted", days)


def _price(option: dict[str, Any]) -> float | None:
    """Bid/ask midpoint when both sides are quoted, else the last trade; ``None`` if neither."""

    def num(key: str) -> float:
        try:
            return float(option.get(key) or 0)
        except TypeError, ValueError:
            return 0.0

    bid, ask = num("bid"), num("ask")
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    last = num("last_trade_price")
    return last if last > 0 else None


def atm_straddle(options: Iterable[dict[str, Any]], expiry: dt.date, spot: float) -> tuple[float, float] | None:
    """``(strike, straddle price)`` at the strike nearest ``spot`` with both legs priced."""
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    for option in options:
        sym = option.get("option", "")
        if len(sym) < 15:
            continue
        try:
            exp, cp, strike = parse_occ_symbol(sym)
        except ValueError, IndexError:
            continue
        if exp != expiry:
            continue
        price = _price(option)
        if price is None:
            continue
        (calls if cp == "C" else puts)[strike] = price
    both = set(calls) & set(puts)
    if not both:
        return None
    strike = min(both, key=lambda k: (abs(k - spot), k))
    return strike, calls[strike] + puts[strike]


def implied_move(
    chain: dict[str, Any],
    *,
    ticker: str,
    event_date: dt.date,
    timing: Timing,
    event_window_days: int = EVENT_WINDOW_DAYS,
    max_days: int = MAX_DAYS,
) -> dict[str, Any]:
    """Implied earnings move for ``ticker``, priced on the event expiry.

    Args:
        chain: ``{current_price, options: [{option, bid, ask, last_trade_price}]}`` with OCC
            option symbols: what :func:`tradekit.data.ibkr.fetch_event_chain` returns (IB
            Gateway, #55), and the shape of CBOE's delayed-quotes ``data``. No I/O here (#49).
        ticker: Underlying symbol.
        event_date: Earnings release date.
        timing: ``"BMO"``, ``"AMC"`` or ``"unknown"``.

    Returns:
        A dict with ``event_expiry``, ``expiry_quality``, ``days_after_event``, ``spot``,
        ``atm_strike``, ``straddle`` and ``implied_move_pct``. When no event expiry exists, or
        it has no priced at-the-money straddle, ``implied_move_pct`` is ``None`` and
        ``expiry_quality`` is ``"unusable"``.
    """
    if not isinstance(chain, dict):
        raise TypeError(f"implied_move takes the chain itself (a dict), not {type(chain).__name__}; fetch it first")
    data = chain
    spot = float(data["current_price"])
    options = data.get("options", [])

    expiries = set()
    for option in options:
        sym = option.get("option", "")
        if len(sym) < 15:
            continue
        try:
            expiries.add(parse_occ_symbol(sym)[0])
        except ValueError, IndexError:
            continue

    choice = select_event_expiry(expiries, event_date, timing, event_window_days=event_window_days, max_days=max_days)
    result: dict[str, Any] = {
        "ticker": ticker.upper(),
        "event_date": event_date.isoformat(),
        "timing": timing,
        "spot": spot,
        "event_expiry": choice.expiry.isoformat() if choice.expiry else None,
        "expiry_quality": choice.quality,
        "days_after_event": choice.days_after_event,
        "atm_strike": None,
        "straddle": None,
        "implied_move_pct": None,
    }
    if choice.expiry is None:
        return result
    priced = atm_straddle(options, choice.expiry, spot)
    if priced is None or spot <= 0:
        result["expiry_quality"] = "unusable"
        return result
    strike, straddle = priced
    result.update(atm_strike=strike, straddle=round(straddle, 4), implied_move_pct=round(straddle / spot * 100.0, 2))
    return result
