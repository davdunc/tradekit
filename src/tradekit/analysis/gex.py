"""Gamma Exposure (GEX) from the CBOE delayed-quotes chain.

Dealer net gamma positioning drives intraday tape character, which is why this
sits upstream of setup selection rather than beside it:

- **Positive GEX** — dealers buy dips and sell rips. Mean-reversion, chop,
  strike-pinning. Fades work; breakouts fail more often than they look like they should.
- **Negative GEX** — dealers sell dips and buy rips. Trends extend, breakouts follow
  through, and a move through a magnet strike can accelerate on hedging flow alone.
- **Zero-gamma flip** — the price at which that positioning changes sign.

Data comes from CBOE's public delayed-quotes CDN: free, no API key, and it carries
real open interest. yfinance was retired for this purpose on 2026-08-11 because its
``openInterest`` returned 0 across every expiry. Gamma is computed with
Black-Scholes-Merton against CBOE's own IV.

.. note::
   **0DTE contracts contribute.** An earlier implementation computed time to expiry
   as ``max((exp - today).days, 0) / 365`` which returned exactly 0 for a same-day
   expiry, and BSM gamma is 0 at T=0 — so every 0DTE contract silently contributed
   nothing. 0DTE is the bulk of SPY options volume; excluding it understated total
   GEX by roughly 40-70% on weekdays. Magnitudes from this implementation are **not
   comparable** to readings taken before that fix. Regime thresholds are unchanged;
   only the measurement got more complete.

.. warning::
   ``zero_gamma_flip`` reports where the running sum across strikes changes sign,
   which is not the same thing as the spot price at which total dealer gamma crosses
   zero. A faithful flip level needs a re-priced spot sweep. Treat the field as
   indicative, not precise.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from tradekit.config import ET

logger = logging.getLogger(__name__)

US_CLOSE = dt.time(16, 0)

#: Floor on time-to-expiry, in seconds. Gamma diverges as T approaches 0, so an
#: unfloored intraday T prints a nonsense number in the closing minutes of an
#: expiry session. Inside ten minutes the gamma is real but unhedgeable — capping
#: there keeps the output decision-useful without pretending the contract is dead.
MIN_T_SECONDS = 600.0

#: Risk-free rate, from the 3-month Treasury bill — the right proxy for a <=30 DTE
#: option, and specifically *not* fed funds. Set 2026-08-30 from the 3-month bill at
#: 3.715%. Re-check when the bill moves ~50bp. (Was 0.0525, stale by ~150bp; ``r``
#: only enters through d1 so the error was second-order, but it was still wrong.)
DEFAULT_RATE = 0.03715

#: Net GEX magnitude, in dollars, above which a regime is called "strong".
#: Absolute thresholds on a book measured in billions have poor resolution — a
#: percentile against a stored history is the better test once one exists.
STRONG_REGIME_THRESHOLD = 100_000_000

#: CBOE publishes cash index roots under an underscore prefix.
CBOE_ROOT = {"SPX": "_SPX", "NDX": "_NDX", "RUT": "_RUT", "VIX": "_VIX"}
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"

_SQRT_2PI = math.sqrt(2.0 * math.pi)


class GEXError(RuntimeError):
    """Raised when a chain cannot be fetched or carries no usable contracts."""


@dataclass
class StrikeGEX:
    """Aggregated call, put and net gamma exposure at a single strike."""

    strike: float
    call_gex: float = 0.0
    put_gex: float = 0.0
    net_gex: float = 0.0

    @property
    def total_magnitude(self) -> float:
        """Sum of absolute call and put exposure — how much gamma sits here at all."""
        return abs(self.call_gex) + abs(self.put_gex)


def _norm_pdf(x: float) -> float:
    """Standard normal PDF.

    Inlined rather than taken from ``scipy.stats.norm`` so this module carries no
    array-library dependency at all: every value here is a scalar.
    """
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def bsm_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes-Merton gamma. ``T`` in years, ``sigma`` annualized."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def years_to_expiry(exp: dt.date, now: dt.datetime) -> Optional[float]:
    """Time to expiry in years, measured to the 16:00 ET close on the expiry date.

    Returns ``None`` once the contract is past its close, so those are dropped rather
    than floored — an after-hours run does not inflate itself on dead contracts.
    Same-day expiries are floored at :data:`MIN_T_SECONDS` and do contribute.
    """
    close = dt.datetime.combine(exp, US_CLOSE, tzinfo=ET)
    seconds = (close - now).total_seconds()
    if seconds <= 0:
        return None
    return max(seconds, MIN_T_SECONDS) / (365.0 * 24 * 3600.0)


def parse_occ_symbol(sym: str) -> tuple[dt.date, str, float]:
    """Parse an OCC option symbol into ``(expiry, call_or_put, strike)``.

    Anchored from the right because the root varies in length:
    ``<root><YYMMDD><C|P><strike * 1000, 8 digits>``.
    """
    strike = int(sym[-8:]) / 1000.0
    cp = sym[-9].upper()
    exp = dt.date(2000 + int(sym[-15:-13]), int(sym[-13:-11]), int(sym[-11:-9]))
    return exp, cp, strike


def fetch_cboe_chain(ticker: str, timeout: int = 25) -> dict[str, Any]:
    """Fetch the CBOE delayed-quotes payload for ``ticker``.

    Returns the inner ``data`` object: ``{current_price, options: [...]}``.
    """
    sym = CBOE_ROOT.get(ticker.upper(), ticker.upper())
    url = CBOE_URL.format(sym=sym)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 — network, JSON and shape errors are one case here
        raise GEXError(f"could not fetch CBOE chain for {ticker}: {exc}") from exc
    if "data" not in payload:
        raise GEXError(f"unexpected CBOE payload for {ticker}: no 'data' key")
    return payload["data"]


def classify_regime(total_net_gex: float) -> tuple[str, str]:
    """Map net GEX to a regime label and its tape-read implication."""
    if total_net_gex > STRONG_REGIME_THRESHOLD:
        return (
            "POSITIVE (strong)",
            "mean-reversion, chop, strike-pinning; fade extremes",
        )
    if total_net_gex > 0:
        return (
            "POSITIVE (moderate)",
            "mild dampening; ranges hold; trends weaker than they look",
        )
    if total_net_gex > -STRONG_REGIME_THRESHOLD:
        return (
            "NEGATIVE (moderate)",
            "mild amplification; trends extend; breakouts more likely to follow through",
        )
    return (
        "NEGATIVE (strong)",
        "amplified moves; momentum extends; chase breakouts, fade fades only at multi-TF confirm",
    )


def compute_gex(
    ticker: str = "SPY",
    max_dte: int = 30,
    rate: float = DEFAULT_RATE,
    *,
    now: dt.datetime | None = None,
    fetch: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute a GEX snapshot for ``ticker``.

    Args:
        ticker: Underlying symbol. Cash index roots are mapped to CBOE's form.
        max_dte: Include expiries within this many days.
        rate: Risk-free rate used in d1.
        now: Evaluation time. Defaults to now in ET. Injectable so a snapshot can be
            recomputed deterministically against a stored chain.
        fetch: Chain fetcher, for tests and for replaying a saved chain. Defaults to
            :func:`fetch_cboe_chain`.

    Returns:
        A dict carrying spot, total net GEX, the regime label and implication, the
        indicative zero-gamma flip, the five strikes holding the most gamma, and
        coverage counts.

    Raises:
        GEXError: if the chain cannot be fetched or holds no usable contracts.
    """
    data = (fetch or fetch_cboe_chain)(ticker)
    try:
        spot = float(data["current_price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GEXError(f"no usable spot price for {ticker}") from exc

    now = now or dt.datetime.now(ET)
    horizon = now.date() + dt.timedelta(days=max_dte)

    strikes: dict[float, StrikeGEX] = {}
    contracts_scanned = 0
    expiries_seen: set[dt.date] = set()
    zero_dte_gex = 0.0

    for option in data.get("options", []):
        sym = option.get("option", "")
        if len(sym) < 15:
            continue
        try:
            exp, cp, strike = parse_occ_symbol(sym)
        except ValueError, IndexError:
            continue
        if exp > horizon:
            continue
        time_to_exp = years_to_expiry(exp, now)
        if time_to_exp is None:
            continue
        try:
            open_interest = int(float(option.get("open_interest", 0) or 0))
            iv = float(option.get("iv", 0) or 0)
        except TypeError, ValueError:
            continue
        if strike <= 0 or iv <= 0 or open_interest <= 0:
            continue

        gamma = bsm_gamma(spot, strike, time_to_exp, rate, iv)
        # gamma x OI x 100 shares/contract x spot^2, divided by 100.
        # The /100 IS the 1% scaling: the result is dollars of dealer gamma per 1%
        # move in the underlying, not per point.
        contract_gex = gamma * open_interest * 100 * spot * spot / 100.0

        bucket = strikes.setdefault(strike, StrikeGEX(strike=strike))
        if cp == "C":
            # Public convention: calls reported positive, dealer assumed short.
            bucket.call_gex += contract_gex
        else:
            # Customers are typically long puts, so dealer gamma is negative there.
            bucket.put_gex += contract_gex
        if exp == now.date():
            zero_dte_gex += contract_gex if cp == "C" else -contract_gex
        contracts_scanned += 1
        expiries_seen.add(exp)

    if not strikes:
        raise GEXError(f"no usable option data for {ticker} — every contract had empty OI or IV")

    for bucket in strikes.values():
        bucket.net_gex = bucket.call_gex - bucket.put_gex

    by_strike = sorted(strikes.values(), key=lambda s: s.strike)
    total_net_gex = sum(s.net_gex for s in by_strike)
    regime, implication = classify_regime(total_net_gex)

    # Indicative flip: walk strikes within +/-15% of spot and find where the running
    # sum changes sign. See the module-level warning about what this is not.
    flip: float | None = None
    cumulative = 0.0
    for bucket in (s for s in by_strike if 0.85 * spot <= s.strike <= 1.15 * spot):
        previous = cumulative
        cumulative += bucket.net_gex
        if previous != 0 and ((previous < 0 < cumulative) or (previous > 0 > cumulative)):
            flip = bucket.strike
            break

    return {
        "ticker": ticker.upper(),
        "spot": spot,
        "asof": now.isoformat(),
        "max_dte": max_dte,
        "rate": rate,
        "total_net_gex": total_net_gex,
        "zero_dte_gex": zero_dte_gex,
        "zero_gamma_flip": flip,
        "regime": regime,
        "implication": implication,
        "top5_strikes": sorted(by_strike, key=lambda s: s.total_magnitude, reverse=True)[:5],
        "contracts_scanned": contracts_scanned,
        "expiries_scanned": len(expiries_seen),
    }


def to_dict(result: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe view of a :func:`compute_gex` result."""
    out = {k: v for k, v in result.items() if k != "top5_strikes"}
    out["top5_strikes"] = [
        {
            "strike": s.strike,
            "call_gex": s.call_gex,
            "put_gex": s.put_gex,
            "net_gex": s.net_gex,
        }
        for s in result["top5_strikes"]
    ]
    return out


def format_markdown(result: dict[str, Any]) -> str:
    """Render a snapshot as the markdown block the morning game plan pastes in."""
    flip = result["zero_gamma_flip"]
    if flip:
        delta = flip - result["spot"]
        flip_str = f"${flip:.2f} ({delta:+.2f} from spot = {delta / result['spot'] * 100:+.2f}%)"
    else:
        flip_str = "no flip in ±15% band (consistent regime)"

    # Only report the 0DTE slice when there is one. After the 16:00 ET close every
    # same-day contract is dropped, and printing "0DTE +0.0M = 0%" there reads as a
    # broken calculation rather than as the correct answer.
    zero_dte = result.get("zero_dte_gex", 0.0)
    net = result["total_net_gex"]
    zero_dte_share = (
        f" (0DTE {zero_dte / 1e6:+,.1f}M = {abs(zero_dte) / abs(net) * 100:.0f}% of net)" if zero_dte and net else ""
    )

    lines = [
        f"### 🌐 GEX Snapshot — {result['ticker']} (max {result['max_dte']} DTE)",
        "",
        f"- **Spot:** ${result['spot']:.2f}",
        f"- **Net dealer GEX:** ${result['total_net_gex'] / 1e6:+,.1f}M  →  **{result['regime']}**{zero_dte_share}",
        f"- **Zero-gamma flip:** {flip_str}",
        f"- **Tape-read implication:** {result['implication']}",
        f"- **Coverage:** {result['contracts_scanned']:,} contracts across {result['expiries_scanned']} expiries",
        "",
        "**Top 5 magnet strikes (by gamma magnitude):**",
        "",
        "| Strike | Call GEX ($M) | Put GEX ($M) | Net ($M) |",
        "|--------|---------------|--------------|----------|",
    ]
    lines += [
        f"| ${s.strike:.2f} | {s.call_gex / 1e6:+.1f} | {s.put_gex / 1e6:+.1f} | {s.net_gex / 1e6:+.1f} |"
        for s in result["top5_strikes"]
    ]
    return "\n".join(lines)
