"""Per-dimension market regime engine. See docs/intents/market-regime.md.

Describes direction, structure, volatility, participation, liquidity, events and data quality
separately, and evaluates playbook eligibility in a separate policy step. Never a trade signal.
"""

from tradekit.regime.classify import SCHEMA_VERSION, assess
from tradekit.regime.config import RegimeConfigError, load_config

__all__ = ["SCHEMA_VERSION", "RegimeConfigError", "assess", "load_config"]
