"""Abstract data provider interface."""

from typing import Protocol

import pandas as pd


class DataProvider(Protocol):
    """Protocol for market data providers."""

    def get_quote(self, ticker: str) -> dict:
        """Get current quote for a ticker."""
        ...

    def get_history(self, ticker: str, period: str = "3mo", interval: str = "1d") -> pd.DataFrame:
        """Get historical OHLCV data."""
        ...

    def get_premarket(self, ticker: str) -> dict:
        """Get pre-market quote data.

        Every provider returns these keys (#51):

        - ``has_premarket`` (bool): there is a premarket price for the current session.
        - ``gap_pct`` (float | None): premarket price vs the previous close, in percent.
          **None when there is no premarket price**, never 0: a 0 is indistinguishable from a
          flat tape, and read as one it hid a closed market from ``comps`` (#48).
        - ``pre_price`` (float | None): the premarket price, None without one.

        A provider that cannot supply premarket data at all may raise ``NotImplementedError``.
        """
        ...
