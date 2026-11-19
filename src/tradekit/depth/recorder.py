"""Depth recorder: runs one or more ``DepthSource`` adapters into a sink, with failover (#9).

Source selection:

- ``auto``: the depth chain, DAS first and IBKR Gateway as the automatic fallback. When the
  preferred source comes back, both run for ``overlap_seconds`` and then the fallback is
  stopped, so a handover has overlap rather than a gap. With no source up, a ``gap_start``
  marker is written and every source is retried; ``gap_end`` when one connects.
- an explicit single source (``das``, ``ibkr``, ``alpaca``): that source only, retried after a
  failure, never substituted.

Alpaca is level 1 (top of book) and is **never** part of ``auto``: silently downgrading from
depth to top-of-book would corrupt the depth dataset. It runs only when selected.

Failover is driven by a source failing to connect, raising, or ending its stream; not by
silence, because a thin symbol can legitimately print nothing for minutes before the open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Protocol

from tradekit.depth.events import Event, Marker

logger = logging.getLogger(__name__)

#: The automatic chain, most preferred first. Alpaca is deliberately absent.
AUTO_CHAIN = ("das", "ibkr")
SOURCES = ("auto", "das", "ibkr", "alpaca")


class PermanentlyUnavailable(RuntimeError):
    """A source can't work in this run (e.g. its SDK isn't installed): report once, then skip it."""


class DepthSource(Protocol):
    """What the recorder needs from a feed. Adapters live in ``tradekit.depth.sources``."""

    name: str

    async def connect(self) -> None:
        """Connect, or raise. Raising means "unavailable": the recorder falls back."""

    async def subscribe(self, symbols: Sequence[str]) -> None: ...

    def events(self) -> AsyncIterator[Event]:
        """Yield events until the connection ends; end or raise on disconnect."""
        ...

    async def close(self) -> None: ...


class Sink(Protocol):
    def add(self, event: Event) -> None: ...

    def flush(self) -> None: ...


class Recorder:
    def __init__(
        self,
        sources: Sequence[DepthSource],
        symbols: Sequence[str],
        sink: Sink,
        *,
        retry_seconds: float = 30.0,
        overlap_seconds: float = 30.0,
    ):
        if not sources:
            raise ValueError("Recorder needs at least one source")
        self.sources = list(sources)  # preference order
        self.symbols = [s.upper() for s in symbols]
        self.sink = sink
        self.retry_seconds = retry_seconds
        self.overlap_seconds = overlap_seconds
        self._active: dict[str, asyncio.Task] = {}
        self._in_gap = False
        self._stop_event = asyncio.Event()
        self._changed = asyncio.Event()  # set when a source's stream ends: fail over now, not later
        self._skipped: set[str] = set()  # reported once, then skipped (DAS until #9 lands, a missing SDK)
        self._last_error: dict[str, str] = {}  # transient errors are marked only when they change

    def _mark(self, marker, source: str, detail: str = "") -> None:
        logger.info("depth %s %s %s", marker, source, detail)
        self.sink.add(Marker(marker, source, detail))

    async def _start(self, source: DepthSource) -> asyncio.Task | None:
        """Connect and subscribe ``source``; return its pump task, or None if it's unavailable."""
        if source.name in self._skipped:
            return None
        try:
            await source.connect()
            await source.subscribe(self.symbols)
        except (NotImplementedError, PermanentlyUnavailable) as e:
            self._skipped.add(source.name)
            self._mark("error", source.name, f"skipped for this run: {e}")
            return None
        except Exception as e:  # noqa: BLE001 - any failure to start means "unavailable"
            detail = f"unavailable: {type(e).__name__}: {e}"
            if self._last_error.get(source.name) != detail:  # retried every pass; marked on change
                self._last_error[source.name] = detail
                self._mark("error", source.name, detail)
            with contextlib.suppress(Exception):
                await source.close()
            return None
        self._last_error.pop(source.name, None)
        describe = getattr(source, "describe", None)
        self._mark("start", source.name, ",".join(self.symbols) + (f" ({describe()})" if describe else ""))
        task = asyncio.create_task(self._pump(source), name=f"depth-{source.name}")
        self._active[source.name] = task
        return task

    async def _pump(self, source: DepthSource) -> None:
        try:
            async for event in source.events():
                self.sink.add(event)
            self._mark("stop", source.name, "stream ended")
        except asyncio.CancelledError:
            self._mark("stop", source.name, "stopped by recorder")
            raise
        except Exception as e:  # noqa: BLE001
            self._mark("error", source.name, f"{type(e).__name__}: {e}")
        finally:
            self._active.pop(source.name, None)
            self._changed.set()
            with contextlib.suppress(Exception):
                await source.close()

    async def _stop(self, name: str) -> None:
        task = self._active.get(name)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _preferred_running(self) -> int | None:
        """Index in ``sources`` of the most preferred running source, if any."""
        for i, s in enumerate(self.sources):
            if s.name in self._active:
                return i
        return None

    async def _wait(self, seconds: float, *also: asyncio.Event) -> None:
        """Sleep up to ``seconds``, waking early on stop or on any of ``also``."""
        waiters = [asyncio.ensure_future(e.wait()) for e in (self._stop_event, *also)]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def run(self, stop: asyncio.Event) -> None:
        """Record until ``stop`` is set. Always flushes, and stops every source, on the way out."""
        self._stop_event = stop
        try:
            while not stop.is_set():
                self._changed.clear()
                await self._step()
                # Wake at the retry interval, or at once if a running source's stream ends.
                await self._wait(self.retry_seconds, self._changed)
        finally:
            for name in list(self._active):
                await self._stop(name)
            self.sink.flush()

    async def _step(self) -> None:
        """One supervision pass: start the best available source, retire a superseded fallback."""
        best = self._preferred_running()
        # Try every source more preferred than the best one running (all of them, if none is).
        candidates = self.sources if best is None else self.sources[:best]
        for i, source in enumerate(candidates):
            if source.name in self._active:
                continue
            if await self._start(source) is None:
                continue
            if self._in_gap:
                self._in_gap = False
                self._mark("gap_end", source.name)
            if best is not None:
                # A more preferred source is back: keep both for the overlap, then retire the rest.
                fallback = self.sources[best].name
                self._mark("switch", source.name, f"from {fallback} after {self.overlap_seconds:g}s overlap")
                await self._wait(self.overlap_seconds)
                if source.name in self._active and not self._stop_event.is_set():
                    for other in self.sources[i + 1 :]:
                        await self._stop(other.name)
            elif i > 0:
                unavailable = ", ".join(s.name for s in self.sources[:i])
                self._mark("switch", source.name, f"fallback: {unavailable} unavailable")
            break
        if not self._active and not self._in_gap:
            self._in_gap = True
            self._mark("gap_start", "", f"no source available: {', '.join(s.name for s in self.sources)}")
        self.sink.flush()


def build_sources(choice: str, factories: dict[str, Callable[[], DepthSource]]) -> list[DepthSource]:
    """Sources for ``--source``: the AUTO_CHAIN for ``auto``, otherwise exactly the one chosen."""
    if choice not in SOURCES:
        raise ValueError(f"unknown source {choice!r}; choose from {', '.join(SOURCES)}")
    names = AUTO_CHAIN if choice == "auto" else (choice,)
    return [factories[n]() for n in names]
