"""Fetch one value per symbol, keeping a failed request as data instead of an exception.

Used where ``analysis`` needs several symbols' data at once (``comps``, #49): the caller
fetches here, in ``data/``, and passes the results in, so the analysis layer does no I/O.
A failed request becomes ``{"error": "..."}`` so the analysis can say *why* a signal is
missing ("premarket request failed") rather than losing the whole report.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)


def fetch_each(fetch: Callable[[str], dict], symbols: Iterable[str], *, into: dict | None = None) -> dict[str, dict]:
    """``{symbol: fetch(symbol)}`` for each distinct symbol, in order. Never raises.

    ``into`` is an existing result to extend: symbols already in it are not fetched again,
    so a command reporting on several tickers fetches shared symbols (QQQ, SPY) once.
    """
    out = into if into is not None else {}
    for symbol in dict.fromkeys(symbols):
        if symbol in out:
            continue
        try:
            out[symbol] = fetch(symbol)
        except Exception as e:  # noqa: BLE001 - any failure is recorded for the caller to report
            logger.warning("fetch failed for %s: %s", symbol, e)
            out[symbol] = {"error": f"{type(e).__name__}: {e}"}
    return out
