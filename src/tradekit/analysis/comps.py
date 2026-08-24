"""Sector & overnight comps — the MU lesson (2026-08-24).

Asia prices US chip/memory names six hours before New York: Samsung's -9%
overnight plunge telegraphed the whole memory complex before the US open,
while a $940 breakout bias fought five visible signals. This module turns
that post-mortem into a premarket step: for a candidate ticker, gather its
overnight comps, sector ETF premarket, inverse-ETF tell, and index skew,
then apply the rule — 3+ signals against the intended direction downgrades
the thesis to counter-trend (explicit reclaim trigger required).
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("tradekit")

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

    @property
    def bearish(self) -> bool | None:
        """True bearish, False bullish, None neutral/unavailable."""
        if self.move_pct is None:
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
        """How many signals oppose the intended direction."""
        return self.bearish_count if direction == "long" else self.bullish_count

    def verdict(self, direction: str) -> str:
        n = self.against(direction)
        if n >= COUNTER_TREND_COUNT:
            return (
                f"COUNTER-TREND: {n} signals against {direction} — "
                "reclaim trigger required, no breakout-bias entries"
            )
        if n > 0:
            return f"CAUTION: {n} signal(s) against {direction}"
        return f"ALIGNED: sector supports {direction}"


def _quote_change_pct(provider, symbol: str) -> float | None:
    try:
        quote = provider.get_quote(symbol)
        price = quote.get("price") or 0
        prev = quote.get("prev_close") or 0
        if price and prev:
            return (price - prev) / prev * 100
    except Exception as e:
        logger.warning("comps: quote failed for %s: %s", symbol, e)
    return None


def _premarket_gap_pct(provider, symbol: str) -> float | None:
    try:
        pm = provider.get_premarket(symbol)
        gap = pm.get("gap_pct")
        return float(gap) if gap is not None else None
    except Exception as e:
        logger.warning("comps: premarket failed for %s: %s", symbol, e)
    return None


def build_comps_report(provider, ticker: str, sector: str | None = None) -> CompsReport | None:
    """Build the sector/overnight comps report for one candidate ticker.

    Returns None when the ticker has no sector mapping (and none was given).
    """
    ticker = ticker.upper()
    key = (sector or TICKER_SECTOR.get(ticker) or "").lower()
    entry = COMP_MAP.get(key)
    if entry is None:
        return None

    report = CompsReport(ticker=ticker, sector=key, sector_label=entry["label"])

    for label, symbol in entry["comps"]:
        pct = _quote_change_pct(provider, symbol)
        report.signals.append(
            Signal(name=f"{label} overnight", move_pct=pct, detail=symbol)
        )

    etf = entry.get("etf")
    if etf:
        pct = _premarket_gap_pct(provider, etf)
        report.signals.append(Signal(name=f"{etf} premarket", move_pct=pct, detail="sector ETF"))

    inverse = entry.get("inverse")
    if inverse:
        pct = _premarket_gap_pct(provider, inverse)
        # An inverse ETF gapping UP is a bearish sector tell: flip its sign
        # so the Signal's bearish/bullish reading stays consistent.
        flipped = -pct if pct is not None else None
        report.signals.append(
            Signal(name=f"{inverse} tell (sign flipped)", move_pct=flipped, detail="inverse ETF")
        )

    qqq = _premarket_gap_pct(provider, "QQQ")
    spy = _premarket_gap_pct(provider, "SPY")
    if qqq is not None and spy is not None:
        # Tech-led skew: meaningful only when QQQ leads SPY; expressed as
        # QQQ's own gap so the threshold logic applies.
        skew = qqq if abs(qqq - spy) >= 0.25 else 0.0
        report.signals.append(
            Signal(
                name="Index skew (QQQ-led)",
                move_pct=skew,
                detail=f"QQQ {qqq:+.2f}% vs SPY {spy:+.2f}%",
            )
        )

    return report
