"""Sector & overnight comps — the MU lesson (2026-08-24).

Asia prices US chip/memory names six hours before New York: Samsung's -9%
overnight plunge telegraphed the whole memory complex before the US open,
while a $940 breakout bias fought five visible signals. This module turns
that post-mortem into a premarket step: for a candidate ticker, gather its
overnight comps, sector ETF premarket, inverse-ETF tell, and index skew,
then apply the rule — 3+ signals against the intended direction downgrades
the thesis to counter-trend (explicit reclaim trigger required).

Freshness (#48). A signal with no data is *unavailable*, never a 0.00% move:

- **Premarket rows.** When the provider says there is no premarket session
  (``has_premarket`` false: before 04:00 ET, weekends), the row is unavailable.
  Yahoo reports that case as ``gap_pct`` 0, which used to read as neutral.
- **Overnight comps.** A comp counts only if its session ended after the most
  recent US close (weekdays 16:00 ET; exchange holidays and early closes are
  not modelled). An older one, such as Friday's Samsung session read on a
  Saturday, is already in the US price, so it is shown as *stale* and not
  counted. A quote without ``market_time`` is counted, labelled "session
  time unknown".
- **The verdict** states how many signals were unavailable or stale, and says
  when they are enough to hide a COUNTER-TREND call. With none live it is
  ``NO READ``, never ``ALIGNED``.
"""

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradekit")

ET = ZoneInfo("America/New_York")
US_CLOSE = dt.time(16, 0)

# Move threshold (%) below/above which a signal counts as bearish/bullish.
SIGNAL_THRESHOLD_PCT = 0.5
# Signals against the intended direction that force a counter-trend verdict.
COUNTER_TREND_COUNT = 3

# Sector key -> comps (label, provider symbol), sector ETF, inverse ETF.
# Overnight comps use Yahoo-style suffixes (.KS = Korea) — closed by US
# premarket, so their regular-session change IS the overnight read.
COMP_MAP: dict[str, dict] = {
    "memory": {
        "label": "Memory",
        "comps": [("Samsung", "005930.KS"), ("SK Hynix", "000660.KS")],
        "etf": "SOXX",
        "inverse": "SOXS",
    },
    "semis": {
        "label": "Semiconductors",
        "comps": [("TSMC", "TSM")],
        "etf": "SOXX",
        "inverse": "SOXS",
    },
    "china": {
        "label": "China / ADR",
        "comps": [("Alibaba", "BABA"), ("Hang Seng ETF", "FXI")],
        "etf": "FXI",
        "inverse": "YANG",
    },
    "crypto": {
        "label": "Crypto-adjacent",
        "comps": [("Bitcoin", "BTC-USD")],
        "etf": "IBIT",
        "inverse": "BITI",
    },
}

# Ticker -> sector key. Extend as tickers recur in the game plan.
TICKER_SECTOR: dict[str, str] = {
    "MU": "memory",
    "SNDK": "memory",
    "WDC": "memory",
    "STX": "memory",
    "NVDA": "semis",
    "AMD": "semis",
    "AVGO": "semis",
    "INTC": "semis",
    "ARM": "semis",
    "MRVL": "semis",
    "SMCI": "semis",
    "SOXL": "semis",
    "TSM": "semis",
    "BABA": "china",
    "JD": "china",
    "PDD": "china",
    "NIO": "china",
    "COIN": "crypto",
    "IBIT": "crypto",
    "MSTR": "crypto",
    "HOOD": "crypto",
    "CONL": "crypto",
}


@dataclass
class Signal:
    """One row of the comps table."""

    name: str
    move_pct: float | None
    detail: str
    note: str = ""  # why it is unavailable or stale, or when its session was
    stale: bool = False  # data from before the last US close: shown, never counted

    @property
    def live(self) -> bool:
        """Has data, and the data is current enough to count."""
        return self.move_pct is not None and not self.stale

    @property
    def bearish(self) -> bool | None:
        """True bearish, False bullish, None neutral, unavailable or stale."""
        if not self.live:
            return None
        if self.move_pct <= -SIGNAL_THRESHOLD_PCT:
            return True
        if self.move_pct >= SIGNAL_THRESHOLD_PCT:
            return False
        return None


@dataclass
class CompsReport:
    ticker: str
    sector: str
    sector_label: str
    signals: list[Signal] = field(default_factory=list)

    @property
    def bearish_count(self) -> int:
        return sum(1 for s in self.signals if s.bearish is True)

    @property
    def bullish_count(self) -> int:
        return sum(1 for s in self.signals if s.bearish is False)

    def against(self, direction: str) -> int:
        """How many signals oppose the intended direction ("long" or "short")."""
        if direction == "long":
            return self.bearish_count
        if direction == "short":
            return self.bullish_count
        # Any other value used to fall through to the short side and give a wrong verdict.
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    @property
    def missing(self) -> int:
        """Signals that are unavailable or stale, so not counted."""
        return sum(1 for s in self.signals if not s.live)

    def verdict(self, direction: str) -> str:
        n = self.against(direction)
        total, missing = len(self.signals), self.missing
        gap = f" ({missing} of {total} signals unavailable or stale)" if missing else ""
        if n >= COUNTER_TREND_COUNT:
            return (
                f"COUNTER-TREND: {n} signals against {direction} — reclaim trigger required, "
                f"no breakout-bias entries{gap}"
            )
        if missing == total:
            # Nothing is live, so nothing is ruled out either, COUNTER-TREND included: say so.
            return (
                f"NO READ: none of the {total} signals is live, so COUNTER-TREND can't be ruled out"
                " — rerun once premarket is trading"
            )
        hidden = ""
        if n + missing >= COUNTER_TREND_COUNT:
            hidden = " — too few live signals to rule out COUNTER-TREND"
        if n > 0:
            return f"CAUTION: {n} signal(s) against {direction}{gap}{hidden}"
        return f"ALIGNED: sector supports {direction}{gap}{hidden}"


def last_us_close(now: dt.datetime) -> dt.datetime:
    """The most recent weekday 16:00 ET at or before ``now``. Holidays are not modelled."""
    now = now.astimezone(ET)
    day = now.date()
    while True:
        close = dt.datetime.combine(day, US_CLOSE, tzinfo=ET)
        if day.weekday() < 5 and close <= now:
            return close
        day -= dt.timedelta(days=1)


def _market_time(value) -> dt.datetime | None:
    """A provider's quote time (epoch seconds or datetime) as an aware datetime, if any."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, int | float) and value > 0:
        return dt.datetime.fromtimestamp(value, dt.UTC)
    return None


def _quote_change(quote: dict | None) -> tuple[float | None, dt.datetime | None]:
    """Close-to-close change (%) and the quote's market time, from a provider quote dict."""
    quote = quote or {}
    price = quote.get("price") or 0
    prev = quote.get("prev_close") or 0
    if price and prev:
        return (price - prev) / prev * 100, _market_time(quote.get("market_time"))
    return None, None


def _premarket_gap(pm: dict | None) -> tuple[float | None, str]:
    """Premarket gap (%), or None and why, from a provider premarket dict.

    ``has_premarket`` false means no session, not 0%. ``{"error": ...}`` is a failed request
    (see ``tradekit.data.batch.fetch_each``).
    """
    if not pm or "error" in pm:
        return None, "premarket request failed"
    if pm.get("has_premarket") is False:
        return None, "no premarket session"
    gap = pm.get("gap_pct")
    if gap is None:
        return None, "no premarket data"
    return float(gap), ""


def _overnight_signal(label: str, symbol: str, pct: float | None, when: dt.datetime | None, close: dt.datetime):
    if pct is None:
        return Signal(f"{label} overnight", None, symbol, note="no quote")
    if when is None:
        return Signal(f"{label} overnight", pct, symbol, note="session time unknown")
    stamp = when.astimezone(ET).strftime("%a %m-%d %H:%M ET")
    if when < close:
        note = f"stale: {stamp}, before the last US close ({close:%a %m-%d})"
        return Signal(f"{label} overnight", pct, symbol, note=note, stale=True)
    return Signal(f"{label} overnight", pct, symbol, note=stamp)


def _entry(ticker: str, sector: str | None) -> tuple[str, dict] | None:
    """The COMP_MAP key and entry for ``ticker``; None if unmapped. Unknown ``sector`` raises."""
    if sector is not None and sector.lower() not in COMP_MAP:
        raise ValueError(f"unknown sector {sector!r}; choose from {', '.join(sorted(COMP_MAP))}")
    key = (sector or TICKER_SECTOR.get(ticker.upper()) or "").lower()
    entry = COMP_MAP.get(key)
    return None if entry is None else (key, entry)


def comps_symbols(ticker: str, sector: str | None = None) -> tuple[list[str], list[str]] | None:
    """The symbols a report needs: (quote symbols, premarket symbols). None if unmapped.

    The caller fetches these (``get_quote`` and ``get_premarket``) and passes the results to
    :func:`build_comps_report`. This module does no I/O (#49). An explicit ``sector`` that
    isn't a COMP_MAP key raises ValueError.
    """
    found = _entry(ticker, sector)
    if found is None:
        return None
    _, entry = found
    quotes = [symbol for _, symbol in entry["comps"]]
    premarket = [s for s in (entry.get("etf"), entry.get("inverse")) if s] + ["QQQ", "SPY"]
    return quotes, premarket


def build_comps_report(
    ticker: str,
    quotes: Mapping[str, dict],
    premarket: Mapping[str, dict],
    sector: str | None = None,
    *,
    now: dt.datetime | None = None,
) -> CompsReport | None:
    """Build the sector/overnight comps report for one candidate ticker, from fetched data.

    ``quotes`` and ``premarket`` map each symbol from :func:`comps_symbols` to what the
    provider's ``get_quote`` / ``get_premarket`` returned, or to ``{"error": ...}`` for a
    failed request. A symbol that is absent counts as unavailable. No I/O happens here.

    Returns None when the ticker has no sector mapping and no ``sector`` was given. An
    explicit ``sector`` that isn't a COMP_MAP key raises ValueError: returning None for it too
    made a mistyped sector read as an unmapped ticker.
    """
    ticker = ticker.upper()
    found = _entry(ticker, sector)
    if found is None:
        return None
    key, entry = found

    report = CompsReport(ticker=ticker, sector=key, sector_label=entry["label"])
    close = last_us_close(now or dt.datetime.now(ET))

    for label, symbol in entry["comps"]:
        pct, when = _quote_change(quotes.get(symbol))
        report.signals.append(_overnight_signal(label, symbol, pct, when, close))

    etf = entry.get("etf")
    if etf:
        pct, why = _premarket_gap(premarket.get(etf))
        report.signals.append(Signal(f"{etf} premarket", pct, "sector ETF", note=why))

    inverse = entry.get("inverse")
    if inverse:
        pct, why = _premarket_gap(premarket.get(inverse))
        # An inverse ETF gapping UP is a bearish sector tell: flip its sign
        # so the Signal's bearish/bullish reading stays consistent.
        flipped = -pct if pct is not None else None
        report.signals.append(Signal(f"{inverse} tell (sign flipped)", flipped, "inverse ETF", note=why))

    qqq, qqq_why = _premarket_gap(premarket.get("QQQ"))
    spy, spy_why = _premarket_gap(premarket.get("SPY"))
    if qqq is not None and spy is not None:
        # Tech-led skew: meaningful only when QQQ leads SPY; expressed as
        # QQQ's own gap so the threshold logic applies.
        skew = qqq if abs(qqq - spy) >= 0.25 else 0.0
        report.signals.append(Signal("Index skew (QQQ-led)", skew, f"QQQ {qqq:+.2f}% vs SPY {spy:+.2f}%"))
    else:
        # Shown as unavailable rather than left out, so the table always has every signal.
        why = qqq_why if qqq is None else spy_why
        report.signals.append(Signal("Index skew (QQQ-led)", None, "QQQ vs SPY", note=why))

    return report
