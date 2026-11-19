"""Depth source adapters (#9): DAS (primary), IBKR Gateway (fallback), Alpaca (level 1, explicit).

Broker SDKs are optional extras (``tradekit-cli[ibkr]``, ``tradekit-cli[alpaca]``) and are
imported only when a source connects, so the base install carries none of them.
"""

from __future__ import annotations

from collections.abc import Callable

from tradekit.depth.recorder import DepthSource, PermanentlyUnavailable


def factories(levels: int = 10) -> dict[str, Callable[[], DepthSource]]:
    """``--source`` name → constructor, reading connection settings at call time."""
    from tradekit.config import get_settings

    data = get_settings().data

    def das() -> DepthSource:
        from tradekit.depth.sources.das import DASSource

        return DASSource()

    def ibkr() -> DepthSource:
        from tradekit.depth.sources.ibkr import IBKRSource

        return IBKRSource(data.ibkr_host, data.ibkr_port, data.ibkr_client_id, levels=levels)

    def alpaca() -> DepthSource:
        from tradekit.depth.sources.alpaca import AlpacaSource

        return AlpacaSource(data.apca_api_key_id, data.apca_api_secret_key, feed=data.alpaca_feed)

    return {"das": das, "ibkr": ibkr, "alpaca": alpaca}


class MissingExtra(PermanentlyUnavailable):
    """A broker SDK isn't installed; the message says which extra to install. Reported once."""

    def __init__(self, source: str, extra: str, package: str):
        super().__init__(
            f"{source} needs {package}: install with `uv tool install 'tradekit-cli[{extra}]'` "
            f"(or `uv sync --extra {extra}` in a checkout)"
        )
