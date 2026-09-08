"""Tests for the gamma exposure snapshot.

Every test injects a synthetic chain — nothing here touches the CBOE CDN.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from tradekit.analysis.gex import (
    DEFAULT_RATE,
    MIN_T_SECONDS,
    STRONG_REGIME_THRESHOLD,
    GEXError,
    bsm_gamma,
    classify_regime,
    compute_gex,
    fetch_cboe_chain,
    format_markdown,
    parse_occ_symbol,
    to_dict,
    years_to_expiry,
)
from tradekit.config import ET

SESSION = dt.datetime(2026, 9, 8, 10, 0, tzinfo=ET)  # mid-session, well before the close


def occ(exp: dt.date, cp: str, strike: float, root: str = "SPY") -> str:
    """Build an OCC symbol the way CBOE writes them."""
    return f"{root}{exp:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def chain(contracts, spot: float = 100.0):
    """Return a fetch() stand-in yielding the given contracts."""
    options = [{"option": occ(exp, cp, strike), "open_interest": oi, "iv": iv} for exp, cp, strike, oi, iv in contracts]
    return lambda _ticker: {"current_price": spot, "options": options}


# --------------------------------------------------------------------------- math


def test_bsm_gamma_peaks_at_the_money():
    """Gamma is maximised near the money and falls away on both sides."""
    atm = bsm_gamma(100.0, 100.0, 0.05, DEFAULT_RATE, 0.20)
    assert atm > bsm_gamma(100.0, 80.0, 0.05, DEFAULT_RATE, 0.20)
    assert atm > bsm_gamma(100.0, 130.0, 0.05, DEFAULT_RATE, 0.20)


def test_bsm_gamma_matches_closed_form():
    """Guard the inlined normal PDF against a hand-computed value."""
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.03715, 0.20
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    expected = (math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)) / (S * sigma * math.sqrt(T))
    assert bsm_gamma(S, K, T, r, sigma) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize(
    "S,K,T,sigma",
    [(100, 100, 0.0, 0.2), (100, 100, -1.0, 0.2), (100, 100, 0.1, 0.0), (0, 100, 0.1, 0.2)],
)
def test_bsm_gamma_degenerate_inputs_are_zero(S, K, T, sigma):
    assert bsm_gamma(S, K, T, DEFAULT_RATE, sigma) == 0.0


# ------------------------------------------------------------------------- expiry


def test_zero_dte_contributes():
    """The regression this module exists to prevent: same-day expiry must not be T=0."""
    t = years_to_expiry(SESSION.date(), SESSION)
    assert t is not None and t > 0
    assert bsm_gamma(100.0, 100.0, t, DEFAULT_RATE, 0.20) > 0


def test_expired_contract_is_dropped_not_floored():
    after_close = dt.datetime(2026, 9, 8, 17, 0, tzinfo=ET)
    assert years_to_expiry(dt.date(2026, 9, 8), after_close) is None


def test_time_to_expiry_is_floored_in_the_closing_minutes():
    one_minute_left = dt.datetime(2026, 9, 8, 15, 59, tzinfo=ET)
    t = years_to_expiry(dt.date(2026, 9, 8), one_minute_left)
    assert t == pytest.approx(MIN_T_SECONDS / (365.0 * 24 * 3600.0))


def test_further_expiry_is_longer():
    near = years_to_expiry(dt.date(2026, 9, 9), SESSION)
    far = years_to_expiry(dt.date(2026, 10, 9), SESSION)
    assert far > near > 0


# --------------------------------------------------------------------------- OCC


@pytest.mark.parametrize(
    "sym,exp,cp,strike",
    [
        ("SPY260908C00767000", dt.date(2026, 9, 8), "C", 767.0),
        ("SPY260911P00760500", dt.date(2026, 9, 11), "P", 760.5),
        ("_SPX261218C05000000", dt.date(2026, 12, 18), "C", 5000.0),
    ],
)
def test_parse_occ_symbol(sym, exp, cp, strike):
    assert parse_occ_symbol(sym) == (exp, cp, strike)


def test_parse_occ_is_right_anchored_across_root_lengths():
    """Roots vary in length; parsing must not depend on it."""
    for root in ("A", "SPY", "GOOGL"):
        exp, cp, strike = parse_occ_symbol(occ(dt.date(2026, 9, 11), "C", 42.5, root))
        assert (exp, cp, strike) == (dt.date(2026, 9, 11), "C", 42.5)


# ------------------------------------------------------------------------- regime


@pytest.mark.parametrize(
    "net,label",
    [
        (STRONG_REGIME_THRESHOLD * 2, "POSITIVE (strong)"),
        (1.0, "POSITIVE (moderate)"),
        (-1.0, "NEGATIVE (moderate)"),
        (-STRONG_REGIME_THRESHOLD * 2, "NEGATIVE (strong)"),
    ],
)
def test_classify_regime(net, label):
    regime, implication = classify_regime(net)
    assert regime == label
    assert implication


def test_classify_regime_zero_is_not_positive():
    assert classify_regime(0.0)[0].startswith("NEGATIVE")


# ------------------------------------------------------------------------ compute


def test_compute_gex_aggregates_by_strike():
    exp = dt.date(2026, 9, 11)
    result = compute_gex(
        "SPY",
        max_dte=30,
        now=SESSION,
        fetch=chain(
            [
                (exp, "C", 100.0, 1_000, 0.20),
                (exp, "C", 100.0, 500, 0.20),  # same strike, should merge
                (exp, "P", 100.0, 400, 0.20),
            ]
        ),
    )
    assert len(result["top5_strikes"]) == 1
    bucket = result["top5_strikes"][0]
    assert bucket.strike == 100.0
    assert bucket.call_gex > 0 and bucket.put_gex > 0
    assert bucket.net_gex == pytest.approx(bucket.call_gex - bucket.put_gex)
    assert result["contracts_scanned"] == 3


def test_puts_drive_net_negative():
    """Heavy put OI produces the negative-gamma regime."""
    exp = dt.date(2026, 9, 11)
    result = compute_gex("SPY", now=SESSION, fetch=chain([(exp, "P", 100.0, 5_000_000, 0.20)]))
    assert result["total_net_gex"] < 0
    assert result["regime"].startswith("NEGATIVE")


def test_contracts_beyond_the_dte_horizon_are_excluded():
    near, far = dt.date(2026, 9, 11), dt.date(2026, 12, 18)
    fetch = chain([(near, "C", 100.0, 1_000, 0.20), (far, "C", 100.0, 1_000, 0.20)])
    assert compute_gex("SPY", max_dte=7, now=SESSION, fetch=fetch)["contracts_scanned"] == 1
    assert compute_gex("SPY", max_dte=180, now=SESSION, fetch=fetch)["contracts_scanned"] == 2


@pytest.mark.parametrize("oi,iv", [(0, 0.20), (1_000, 0.0), (None, 0.20), (1_000, None)])
def test_contracts_without_oi_or_iv_are_skipped(oi, iv):
    exp = dt.date(2026, 9, 11)
    fetch = chain([(exp, "C", 100.0, oi, iv), (exp, "C", 105.0, 1_000, 0.20)])
    assert compute_gex("SPY", now=SESSION, fetch=fetch)["contracts_scanned"] == 1


def test_zero_dte_bucket_is_reported_separately():
    today, later = SESSION.date(), dt.date(2026, 10, 2)  # both inside the 30-day horizon
    result = compute_gex(
        "SPY",
        now=SESSION,
        fetch=chain([(today, "C", 100.0, 2_000, 0.20), (later, "C", 100.0, 2_000, 0.20)]),
    )
    assert result["contracts_scanned"] == 2
    assert result["zero_dte_gex"] > 0
    # The 0DTE slice is part of the net, not all of it.
    assert result["zero_dte_gex"] < result["total_net_gex"]
    # And it dominates: ATM gamma is far larger at one day than at three weeks.
    assert result["zero_dte_gex"] > 0.5 * result["total_net_gex"]


def test_empty_chain_raises_gex_error_not_systemexit():
    """A library must not kill the process — the old tool called SystemExit here."""
    with pytest.raises(GEXError):
        compute_gex("SPY", now=SESSION, fetch=chain([]))


def test_malformed_symbols_are_ignored():
    exp = dt.date(2026, 9, 11)
    fetch = lambda _t: {  # noqa: E731
        "current_price": 100.0,
        "options": [
            {"option": "SHORT", "open_interest": 10, "iv": 0.2},
            {"option": "SPYnotadate C00100000", "open_interest": 10, "iv": 0.2},
            {"option": occ(exp, "C", 100.0), "open_interest": 1_000, "iv": 0.20},
        ],
    }
    assert compute_gex("SPY", now=SESSION, fetch=fetch)["contracts_scanned"] == 1


def test_missing_spot_raises():
    with pytest.raises(GEXError):
        compute_gex("SPY", now=SESSION, fetch=lambda _t: {"options": []})


def test_network_failure_is_wrapped_as_gex_error(monkeypatch):
    """Transport errors surface as GEXError, so the CLI can report them cleanly."""

    def boom(*_args, **_kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr("tradekit.analysis.gex.urllib.request.urlopen", boom)
    with pytest.raises(GEXError, match="could not fetch"):
        fetch_cboe_chain("SPY")


def test_payload_without_data_key_is_wrapped(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return b'{"unexpected": true}'

    monkeypatch.setattr("tradekit.analysis.gex.urllib.request.urlopen", lambda *_a, **_k: FakeResponse())
    with pytest.raises(GEXError, match="no 'data' key"):
        fetch_cboe_chain("SPY")


# ------------------------------------------------------------------------- output


def test_format_markdown_shape():
    exp = dt.date(2026, 9, 11)
    result = compute_gex("SPY", now=SESSION, fetch=chain([(exp, "P", 100.0, 5_000_000, 0.20)]))
    md = format_markdown(result)
    assert md.startswith("### 🌐 GEX Snapshot — SPY")
    assert "| Strike | Call GEX ($M) | Put GEX ($M) | Net ($M) |" in md
    assert "no flip in ±15% band" in md
    assert "NEGATIVE" in md


def test_to_dict_is_json_serialisable():
    import json

    exp = dt.date(2026, 9, 11)
    result = compute_gex("SPY", now=SESSION, fetch=chain([(exp, "C", 100.0, 1_000, 0.20)]))
    payload = json.loads(json.dumps(to_dict(result), default=str))
    assert payload["ticker"] == "SPY"
    assert isinstance(payload["top5_strikes"], list)
    assert set(payload["top5_strikes"][0]) == {"strike", "call_gex", "put_gex", "net_gex"}


def test_zero_dte_clause_omitted_when_there_is_no_zero_dte():
    """After the close there is no 0DTE slice — say nothing rather than '0%'."""
    result = compute_gex("SPY", now=SESSION, fetch=chain([(dt.date(2026, 9, 25), "C", 100.0, 1_000, 0.20)]))
    assert result["zero_dte_gex"] == 0.0
    assert "0DTE" not in format_markdown(result)


def test_zero_dte_clause_present_when_it_contributes():
    result = compute_gex("SPY", now=SESSION, fetch=chain([(SESSION.date(), "C", 100.0, 1_000, 0.20)]))
    assert "0DTE" in format_markdown(result)
