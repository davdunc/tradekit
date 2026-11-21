"""Pre-market gap and volume scanner — the core morning workflow module."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from tradekit.config import Settings
from tradekit.data.finviz import FinvizProvider
from tradekit.data.yahoo import YahooProvider
from tradekit.errors import EnrichmentError
from tradekit.screener.filters import apply_filters, build_filter_chain

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)


# Seed-list liquidity floor (average daily shares). Below this a big % change is usually a
# handful of off-hours shares, not a candidate.
MIN_SEED_AVG_VOLUME = 100_000

#: Candidates re-checked with ``get_quote`` before calling an empty enrichment a tool failure.
ENRICHMENT_PROBE = 3


def _sample(tickers: list[str], n: int = 8) -> str:
    """``A, B, C (+5 more)`` for log lines, so the symbols actually attempted are visible."""
    more = f" (+{len(tickers) - n} more)" if len(tickers) > n else ""
    return ", ".join(tickers[:n]) + more


def _resolves(provider: Any, ticker: str) -> bool:
    """True if ``provider`` knows ``ticker``: a listed symbol always has a previous close."""
    try:
        quote = provider.get_quote(ticker) or {}
    except Exception:  # noqa: BLE001 - any provider error means "did not resolve"
        return False
    try:
        return float(quote.get("prev_close") or 0) > 0
    except TypeError, ValueError:
        return False


def finviz_gainers(min_price: float, limit: int | None = 50) -> pd.DataFrame:
    """Top gainers, from the cached Elite universe when a token is set, else the public scraper.

    The universe path is one shared export filtered locally, so ``scan`` and ``second-day`` in
    the same TTL window cost a single Finviz request. A ``FinvizThrottled`` from either path
    propagates — it means "unknown", and must not be reported as "no candidates".
    """
    from tradekit.data.finviz_elite import FinvizEliteConfig, FinvizEliteProvider, top_gainers

    cfg = FinvizEliteConfig.from_env()
    if cfg is not None:
        return top_gainers(
            FinvizEliteProvider(cfg).get_universe(),
            min_price=min_price,
            limit=limit,
            min_avg_volume=MIN_SEED_AVG_VOLUME,
        )
    logger.info("No FINVIZ_AUTH_TOKEN — falling back to the public Finviz screener")
    return FinvizProvider().get_top_gainers(min_price=min_price)


def scan_premarket(
    settings: Settings | None = None,
    preset: str = "premarket_gap",
    provider: Any = None,
) -> pd.DataFrame:
    """Run the pre-market scanner.

    1. Pull top gainers from Finviz for initial candidate list.
    2. Enrich with Yahoo pre-market data (gap %, volume, float).
    3. Apply configurable filters.
    4. Return filtered, sorted candidates.

    Args:
        settings: App settings. Uses defaults if None.
        preset: Name of screener preset from config/screener.yaml.

    Returns:
        DataFrame of pre-market candidates sorted by gap%.
    """
    if settings is None:
        settings = Settings()

    presets = settings.load_screener_presets()
    filter_config = presets.get(preset, {})

    # Step 1: Get initial candidates from Finviz
    logger.info("Fetching top gainers from Finviz...")
    finviz_df = finviz_gainers(min_price=filter_config.get("min_price", settings.screener.min_price))

    # Extract tickers from Finviz results
    tickers: list[str] = []
    if not finviz_df.empty and "Ticker" in finviz_df.columns:
        tickers = finviz_df["Ticker"].tolist()
    elif not finviz_df.empty:
        # Try lowercase column name
        for col in finviz_df.columns:
            if col.lower() == "ticker":
                tickers = finviz_df[col].tolist()
                break

    if not tickers:
        logger.warning("No candidates found from Finviz screener")
        return pd.DataFrame()

    logger.info("Found %d Finviz candidates, enriching with market data: %s", len(tickers), _sample(tickers))

    # Step 2: Enrich with pre-market data from the chosen provider
    if provider is None:
        provider = YahooProvider()
    premarket_data = provider.get_multiple_premarket(tickers)

    if not premarket_data:
        # No pre-market rows can mean "no pre-market trades yet" (early, thin names) or "none of
        # these symbols resolve" (#16's doubled tickers, an expired key, an outage). Only the
        # second is a broken tool, so re-check a few candidates before calling it empty.
        if not any(_resolves(provider, t) for t in tickers[:ENRICHMENT_PROBE]):
            raise EnrichmentError(tickers)
        logger.warning("No pre-market data retrieved: symbols resolve, but none has pre-market activity yet")
        return pd.DataFrame()

    df = pd.DataFrame(premarket_data)

    # Step 3: Apply filters
    filters = build_filter_chain(filter_config)
    df = apply_filters(df, filters)

    if df.empty:
        logger.info("No candidates passed filters")
        return df

    # Step 4: Sort by absolute gap percentage
    df = df.sort_values("gap_pct", key=abs, ascending=False).reset_index(drop=True)

    max_results = filter_config.get("max_results", settings.screener.max_results)
    return df.head(max_results)


def scan_previous_movers(
    provider: Any = None,
    min_change_pct: float = 5.0,
    min_volume_ratio: float = 1.5,
) -> pd.DataFrame:
    """Find stocks that moved >min_change_pct yesterday with above-avg volume.

    Uses Finviz top gainers/losers from the previous session, then enriches
    with Yahoo pre-market data for continuation signals.

    Returns:
        DataFrame with columns: ticker, name, prev_close, prev_change_pct,
        volume_ratio, pre_price, pre_gap_pct (None without a premarket).
    """
    if provider is None:
        provider = YahooProvider()

    logger.info("Fetching top gainers for 2nd-day scan...")
    gainers_df = finviz_gainers(min_price=3.0)

    tickers: list[str] = []
    if not gainers_df.empty:
        col = next((c for c in gainers_df.columns if c.lower() == "ticker"), None)
        if col:
            tickers = gainers_df[col].tolist()

    if not tickers:
        logger.warning("No previous movers found from Finviz")
        return pd.DataFrame()

    attempted = tickers[:20]
    logger.info("Enriching %d candidates with market data: %s", len(attempted), _sample(attempted))
    rows = []
    for ticker in attempted:
        try:
            quote = provider.get_quote(ticker)
            price = quote.get("price", 0)
            prev_close = quote.get("prev_close", 0)
            volume = quote.get("volume", 0)
            avg_volume = quote.get("avg_volume", 1)

            if not prev_close:
                continue

            change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
            vol_ratio = volume / avg_volume if avg_volume else 0

            # Get pre-market data for continuation signal
            pre = provider.get_premarket(ticker)

            rows.append(
                {
                    "ticker": ticker,
                    "name": quote.get("name", ticker),
                    "price": price,
                    "prev_close": prev_close,
                    "prev_change_pct": round(change_pct, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "pre_price": pre.get("pre_price"),
                    "pre_gap_pct": pre.get("gap_pct"),  # None without a premarket (#51), never 0
                    "has_premarket": pre.get("has_premarket", False),
                }
            )
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", ticker, e)

    if not rows:
        # A row needs a quote with a previous close, so no rows means no candidate resolved.
        raise EnrichmentError(attempted)

    df = pd.DataFrame(rows)

    # Filter by minimum change and volume ratio
    df = df[df["prev_change_pct"].abs() >= min_change_pct]
    if not df.empty and min_volume_ratio > 0:
        df = df[df["volume_ratio"] >= min_volume_ratio]

    df = df.sort_values("prev_change_pct", key=abs, ascending=False).reset_index(drop=True)
    return df


def scan_watchlist(
    settings: Settings | None = None,
    watchlist_name: str = "default",
    provider: Any = None,
) -> pd.DataFrame:
    """Scan watchlist tickers for pre-market activity.

    Args:
        settings: App settings.
        watchlist_name: Which watchlist to scan.

    Returns:
        DataFrame of watchlist tickers with pre-market data.
    """
    if settings is None:
        settings = Settings()

    watchlists = settings.load_watchlists()
    tickers = watchlists.get(watchlist_name, [])

    if not tickers:
        logger.warning("Watchlist '%s' is empty or not found", watchlist_name)
        return pd.DataFrame()

    if provider is None:
        provider = YahooProvider()
    premarket_data = provider.get_multiple_premarket(tickers)

    if not premarket_data:
        return pd.DataFrame()

    df = pd.DataFrame(premarket_data)
    df = df.sort_values("gap_pct", key=abs, ascending=False).reset_index(drop=True)
    return df
