"""Buffered, crash-safe Parquet writer for recorder events.

Layout under the root (Hive-style, so pandas / pyarrow / DuckDB read it as one dataset)::

    <root>/book/date=YYYY-MM-DD/source=<src>/symbol=<SYM>/part-<ns>.parquet
    <root>/trades/date=YYYY-MM-DD/source=<src>/symbol=<SYM>/part-<ns>.parquet
    <root>/markers/date=YYYY-MM-DD/source=<src>/part-<ns>.parquet

The date is the event's UTC receive date. Each flush writes a new part file to a temporary
name and renames it into place, so a crash can lose at most the unflushed buffer, never
corrupt a file already written. Local files are the store half of store-and-forward: they
survive a network partition or a service restart, which a synchronous push would not.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tradekit.depth.events import Event

logger = logging.getLogger(__name__)


class ParquetSink:
    def __init__(
        self,
        root: Path,
        *,
        flush_rows: int = 5_000,
        flush_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.root = Path(root)
        self.flush_rows = flush_rows
        self.flush_seconds = flush_seconds
        self._clock = clock
        self._buffers: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
        self._pending = 0
        self._last_flush = clock()
        self.rows_written = 0

    def add(self, event: Event) -> None:
        row = event.row()
        key = (event.kind, row["ts"].date().isoformat(), row["source"], getattr(event, "symbol", "") or "")
        self._buffers[key].append(row)
        self._pending += 1
        if self._pending >= self.flush_rows or self._clock() - self._last_flush >= self.flush_seconds:
            self.flush()

    def flush(self) -> None:
        for (kind, date, source, symbol), rows in self._buffers.items():
            if rows:
                self._write(kind, date, source, symbol, rows)
        self._buffers.clear()
        self._pending = 0
        self._last_flush = self._clock()

    def close(self) -> None:
        self.flush()

    def _write(self, kind: str, date: str, source: str, symbol: str, rows: list[dict]) -> None:
        part_dir = self.root / kind / f"date={date}" / f"source={source}"
        if symbol:
            part_dir = part_dir / f"symbol={symbol}"
        part_dir.mkdir(parents=True, exist_ok=True)
        name = f"part-{time.time_ns()}"
        tmp = part_dir / f".{name}.parquet.tmp"
        final = part_dir / f"{name}.parquet"
        try:
            pq.write_table(pa.Table.from_pylist(rows), tmp)
            os.replace(tmp, final)
        except BaseException:
            # Readers only see *.parquet, so a leftover temp file isn't data, but it is clutter
            # that grows with every failed write. Remove it, then let the failure surface.
            tmp.unlink(missing_ok=True)
            raise
        self.rows_written += len(rows)
        logger.debug("wrote %d %s rows to %s", len(rows), kind, final)
