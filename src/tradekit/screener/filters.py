"""Composable filter chain for stock screening."""

from collections.abc import Callable

import pandas as pd

FilterFunc = Callable[[pd.DataFrame], pd.DataFrame]


def price_filter(min_price: float = 0, max_price: float = float("inf")) -> FilterFunc:
    """Filter by price range."""

    def _filter(df: pd.DataFrame) -> pd.DataFrame:
        if "pre_price" not in df.columns and "price" not in df.columns:
            return df
        # The premarket price when there is one, else the regular price (#51): a missing
        # premarket price is None, not 0, and must not decide whether the row passes.
        price = pd.to_numeric(df["pre_price"], errors="coerce") if "pre_price" in df.columns else None
        if "price" in df.columns:
            regular = pd.to_numeric(df["price"], errors="coerce")
            price = regular if price is None else price.where(price.notna() & (price > 0), regular)
        return df[price.notna() & (price >= min_price) & (price <= max_price)]

    return _filter


def volume_filter(min_volume: int = 0, column: str = "pre_volume") -> FilterFunc:
    """Filter by minimum volume."""

    def _filter(df: pd.DataFrame) -> pd.DataFrame:
        if column not in df.columns:
            return df
        return df[df[column] >= min_volume]

    return _filter


def gap_filter(min_gap_pct: float = 0) -> FilterFunc:
    """Filter by minimum gap percentage (absolute value). A missing gap (no premarket) never passes."""

    def _filter(df: pd.DataFrame) -> pd.DataFrame:
        if "gap_pct" not in df.columns:
            return df
        gap = pd.to_numeric(df["gap_pct"], errors="coerce")
        return df[gap.notna() & (gap.abs() >= min_gap_pct)]

    return _filter


def avg_volume_filter(min_avg_volume: int = 0) -> FilterFunc:
    """Filter by minimum average daily volume."""

    def _filter(df: pd.DataFrame) -> pd.DataFrame:
        if "avg_volume" not in df.columns:
            return df
        return df[df["avg_volume"] >= min_avg_volume]

    return _filter


def float_filter(max_float_millions: float = float("inf")) -> FilterFunc:
    """Filter by maximum float size in millions."""

    def _filter(df: pd.DataFrame) -> pd.DataFrame:
        if "float_shares" not in df.columns:
            return df
        return df[df["float_shares"] <= max_float_millions * 1_000_000]

    return _filter


def apply_filters(df: pd.DataFrame, filters: list[FilterFunc]) -> pd.DataFrame:
    """Apply a chain of filters sequentially."""
    result = df.copy()
    for f in filters:
        result = f(result)
        if result.empty:
            break
    return result


def build_filter_chain(config: dict) -> list[FilterFunc]:
    """Build a filter chain from a screener config dict."""
    filters: list[FilterFunc] = []

    if "min_price" in config or "max_price" in config:
        filters.append(
            price_filter(
                min_price=config.get("min_price", 0),
                max_price=config.get("max_price", float("inf")),
            )
        )

    if "min_premarket_volume" in config:
        filters.append(
            volume_filter(
                min_volume=config["min_premarket_volume"],
                column="pre_volume",
            )
        )

    if "min_gap_pct" in config:
        filters.append(gap_filter(min_gap_pct=config["min_gap_pct"]))

    if "min_avg_volume" in config:
        filters.append(avg_volume_filter(min_avg_volume=config["min_avg_volume"]))

    if "max_float_millions" in config:
        filters.append(float_filter(max_float_millions=config["max_float_millions"]))

    return filters
