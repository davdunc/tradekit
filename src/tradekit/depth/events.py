"""The recorder's normalized events: one shape whichever source produced them (#9).

Every event carries ``source`` so analyses can filter or compare feeds, and ``ts`` is the
time *this machine* received it (UTC), which is the only clock all sources share.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Literal

Side = Literal["bid", "ask"]
Op = Literal["insert", "update", "delete"]
MarkerKind = Literal["start", "stop", "switch", "gap_start", "gap_end", "error"]

#: ``depth_levels`` value for a source that delivers the full book it is subscribed to.
FULL_DEPTH = 0
#: ``position`` for a row keyed by venue, not by level: one venue's own best bid or ask (DAS's
#: Level 2 montage). Rebuild that book from (side, venue); there is no level index to report.
VENUE_KEYED = -1


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class BookUpdate:
    """One change to one price level of one side of the book.

    ``position`` is the level index (0 = best), or ``VENUE_KEYED`` when the source keys its book
    by venue (DAS). ``depth_levels`` is how many levels the source
    can deliver at all: ``FULL_DEPTH`` for a depth feed, 1 for a top-of-book feed such as
    Alpaca, so level 1 data can never pass for depth.
    """

    source: str
    symbol: str
    side: Side
    position: int
    op: Op
    price: float | None
    size: float | None
    depth_levels: int = FULL_DEPTH
    venue: str = ""
    ts: dt.datetime = field(default_factory=utcnow)

    kind = "book"

    def row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Trade:
    """One trade print."""

    source: str
    symbol: str
    price: float
    size: float
    venue: str = ""
    ts: dt.datetime = field(default_factory=utcnow)

    kind = "trades"

    def row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Marker:
    """A recorder event: a source started or stopped, a switch, the start or end of a gap.

    Gaps are written explicitly so "no rows" is never mistaken for "no activity".
    """

    marker: MarkerKind
    source: str
    detail: str = ""
    ts: dt.datetime = field(default_factory=utcnow)

    kind = "markers"
    symbol = ""

    def row(self) -> dict:
        return asdict(self)


Event = BookUpdate | Trade | Marker
