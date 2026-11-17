"""Errors that mean "results are unknown", kept dependency-free so the CLI can import them eagerly."""

from __future__ import annotations


class EnrichmentError(RuntimeError):
    """The screener found candidates, but the data provider resolved none of them (#17).

    That is a dead tool (bad symbols, an expired key, an outage), not a quiet tape, so it is
    raised instead of returning an empty result. The CLI reports it as UNAVAILABLE, exit 3.
    """

    def __init__(self, attempted: list[str]):
        self.attempted = list(attempted)
        sample = ", ".join(self.attempted[:5])
        super().__init__(
            f"0/{len(self.attempted)} candidates enriched: the data provider resolved none of them "
            f"(attempted: {sample}). This is a tool failure, not an empty market"
        )
