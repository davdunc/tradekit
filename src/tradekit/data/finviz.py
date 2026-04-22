"""Finviz screener data provider."""

import datetime
import json
import logging
import os
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from finvizfinance.screener.overview import Overview

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Finviz filter mappings
SIGNAL_MAP = {
    "top_gainers": "Top Gainers",
    "new_high": "New High",
    "most_volatile": "Most Volatile",
    "most_active": "Most Active",
    "unusual_volume": "Unusual Volume",
    "overbought": "Overbought",
    "oversold": "Oversold",
}

# Finviz only accepts these exact price thresholds
_PRICE_THRESHOLDS = [1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100]

_VOLUME_OPTIONS = {
    50_000: "Over 50K",
    100_000: "Over 100K",
    200_000: "Over 200K",
    300_000: "Over 300K",
    400_000: "Over 400K",
    500_000: "Over 500K",
    750_000: "Over 750K",
    1_000_000: "Over 1M",
    2_000_000: "Over 2M",
}


def _nearest_price_over(price: float) -> str:
    """Map a price to the nearest valid Finviz 'Over $X' filter."""
    # Find the largest threshold <= the requested price
    valid = [t for t in _PRICE_THRESHOLDS if t <= price]
    threshold = valid[-1] if valid else _PRICE_THRESHOLDS[0]
    return f"Over ${threshold}"


def _nearest_volume_over(volume: int) -> str:
    """Map a volume to the nearest valid Finviz volume filter."""
    thresholds = sorted(_VOLUME_OPTIONS.keys())
    valid = [t for t in thresholds if t <= volume]
    threshold = valid[-1] if valid else thresholds[0]
    return _VOLUME_OPTIONS[threshold]


class FinvizProvider:
    """Fetch screener data from Finviz."""

    def screen(
        self,
        signal: str = "",
        min_price: float | None = None,
        max_price: float | None = None,
        min_volume: int | None = None,
        min_market_cap: str | None = None,
    ) -> pd.DataFrame:
        """Run a Finviz screener query and return results as DataFrame.

        Args:
            signal: One of the SIGNAL_MAP keys, or empty for custom filters.
            min_price: Minimum stock price.
            max_price: Maximum stock price.
            min_volume: Minimum average volume.
            min_market_cap: e.g. "+Small (over $300mln)" or "+Mid (over $2bln)".

        Returns:
            DataFrame with columns: No., Ticker, Company, Sector, Industry,
            Country, Market Cap, P/E, Price, Change, Volume.
        """
        screener = Overview()

        filters_dict: dict[str, str] = {}
        if min_price is not None:
            filters_dict["Price"] = _nearest_price_over(min_price)

        if min_volume is not None:
            filters_dict["Average Volume"] = _nearest_volume_over(min_volume)

        if min_market_cap:
            filters_dict["Market Cap."] = min_market_cap

        if filters_dict:
            screener.set_filter(filters_dict=filters_dict)

        if signal and signal in SIGNAL_MAP:
            screener.set_filter(signal=SIGNAL_MAP[signal])

        try:
            df = screener.screener_view()
            if df is None or df.empty:
                return pd.DataFrame()
            return df
        except Exception as e:
            logger.warning("Finviz screener failed: %s", e)
            return pd.DataFrame()

    def get_top_gainers(self, min_price: float = 5.0) -> pd.DataFrame:
        """Get today's top gaining stocks."""
        return self.screen(signal="top_gainers", min_price=min_price)

    def get_unusual_volume(self, min_price: float = 5.0) -> pd.DataFrame:
        """Get stocks with unusual volume."""
        return self.screen(signal="unusual_volume", min_price=min_price)

    def get_most_active(self, min_price: float = 5.0) -> pd.DataFrame:
        """Get most actively traded stocks."""
        return self.screen(signal="most_active", min_price=min_price)

    def get_market_news(self, api_key: str = "") -> list[dict]:
        """Fetch Market Pulse news from Finviz Elite (news.ashx?v=6).

        Args:
            api_key: Finviz Elite API key. Falls back to FINVIZ_API_KEY env var.

        Returns:
            List of dicts with keys: timestamp, headline, source, tickers,
            sentiment, url.
        """
        key = api_key or os.environ.get("FINVIZ_API_KEY", "")
        if not key:
            logger.warning("No FINVIZ_API_KEY set — cannot fetch market news")
            return []

        url = f"https://elite.finviz.com/news.ashx?v=6&auth={key}"
        headers = {"User-Agent": _BROWSER_UA}

        try:
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Finviz news fetch failed: %s", e)
            return []

        # Save debug HTML on first run for selector refinement
        cache_dir = Path.home() / ".tradekit" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        debug_path = cache_dir / "finviz_news_debug.html"
        debug_path.write_text(resp.text, encoding="utf-8")
        logger.debug("Saved debug HTML to %s", debug_path)

        return _parse_news_html(resp.text)


def _trade_review_day_dir(date: datetime.date | None = None) -> Path:
    """Return Trade_Review/YYYY/MM/YYYY-MM-DD/ directory for the given date."""
    from tradekit.config import now_et

    if date is None:
        date = now_et().date()
    base = Path(
        os.environ.get(
            "TRADE_REVIEW_PATH",
            str(Path.home() / "OneDrive" / "Documents" / "Trade_Review"),
        )
    )
    return base / str(date.year) / f"{date.month:02d}" / str(date)


def save_news(items: list[dict], date: datetime.date | None = None) -> Path:
    """Persist news items to Trade_Review day directory as news.json."""
    day_dir = _trade_review_day_dir(date)
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "news.json"
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")
    logger.info("Saved %d news items to %s", len(items), path)
    return path


def load_news(date: datetime.date | None = None) -> list[dict]:
    """Load previously saved news items from Trade_Review day directory."""
    path = _trade_review_day_dir(date) / "news.json"
    if not path.exists():
        logger.debug("No saved news at %s", path)
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_news_html(html: str) -> list[dict]:
    """Parse Finviz Market Pulse HTML into structured news items.

    The page uses a table with class ``styled-table-new`` inside ``<div id="news">``.
    Each row has:
      - ``td.news_date-cell``  — relative time ("39 sec", "5 min", "2 hr")
      - ``td.news_link-cell``  — headline span + ticker badge links
    Sentiment is encoded in ticker badge CSS: ``is-positive-*`` / ``is-negative-*``.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Primary: table inside the #news div
    news_div = soup.find("div", id="news")
    table = news_div.find("table") if news_div else None

    # Fallback: find rows with the market-pulse class directly
    if table is None:
        rows = soup.find_all("tr", class_="news_table-row")
        if not rows:
            logger.warning("Could not find news content in Finviz HTML")
            return []
    else:
        rows = table.find_all("tr", class_="news_table-row")

    results: list[dict] = []

    for row in rows:
        # Headline
        headline_el = row.find("span", class_="market-pulse-headline")
        if headline_el is None:
            continue
        headline = headline_el.get("title") or headline_el.get_text(strip=True)

        # Time
        time_cell = row.find("td", class_="news_date-cell")
        timestamp = time_cell.get_text(strip=True) if time_cell else ""

        # Type (from icon tooltip)
        icon_el = row.find("span", class_="market-pulse-icon")
        source = ""
        if icon_el:
            tooltip = icon_el.get("data-boxover-html", "")
            if "detective" in tooltip.lower() or "uncovers" in tooltip.lower():
                source = "Detective"
            elif "market" in tooltip.lower() and "movement" in tooltip.lower():
                source = "Market"
            elif "company news" in tooltip.lower():
                source = "News"
            elif "summary" in tooltip.lower():
                source = "Summary"

        # Tickers and sentiment from badge links
        tickers: list[str] = []
        sentiment = "NEUT"
        badge_div = row.find("div", class_="market-pulse-badges")
        if badge_div:
            for a in badge_div.find_all("a", href=True):
                ticker_attr = a.get("data-boxover-ticker")
                if ticker_attr:
                    tickers.append(ticker_attr.upper())
                else:
                    href = a.get("href", "")
                    if "quote.ashx" in href:
                        # Extract from ?t=TICKER
                        import re

                        m = re.search(r"[?&]t=([A-Z]+)", href)
                        if m:
                            tickers.append(m.group(1))

                # Sentiment from badge CSS classes
                classes = " ".join(a.get("class", []))
                if "is-positive" in classes:
                    sentiment = "BULL"
                elif "is-negative" in classes:
                    sentiment = "BEAR"

        # Detail URL from the row's data-wiim-trigger (Finviz internal ID)
        wiim_id = row.get("data-wiim-trigger", "")
        url = f"https://finviz.com/wiim.ashx?id={wiim_id}" if wiim_id else ""

        results.append(
            {
                "timestamp": timestamp,
                "headline": headline,
                "source": source,
                "tickers": tickers,
                "sentiment": sentiment,
                "url": url,
            }
        )

    return results
