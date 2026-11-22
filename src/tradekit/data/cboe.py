"""CBOE delayed-quotes option chain: the only network access behind ``gex`` and ``earnings``.

Free, no API key, and it carries real open interest (yfinance was retired for chains on
2026-08-11 because its ``openInterest`` read 0 across every expiry). Quotes are **delayed**:
CBOE's public CDN, typically about 15 minutes behind.

Lives in ``data/`` so that ``analysis/`` stays pure (SPEC, #49): ``analysis.gex.compute_gex``
and ``analysis.earnings.implied_move`` take the chain this returns, so a test or a replay of a
saved chain needs no network at all.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

#: CBOE publishes cash index roots under an underscore prefix.
CBOE_ROOT = {"SPX": "_SPX", "NDX": "_NDX", "RUT": "_RUT", "VIX": "_VIX"}
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"


class CBOEError(RuntimeError):
    """The chain could not be fetched, or the payload has no ``data``."""


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
        raise CBOEError(f"could not fetch CBOE chain for {ticker}: {exc}") from exc
    if "data" not in payload:
        raise CBOEError(f"unexpected CBOE payload for {ticker}: no 'data' key")
    return payload["data"]
