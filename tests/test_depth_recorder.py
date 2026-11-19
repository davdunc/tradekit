"""Depth recorder source selection and failover (#9). Scripted fake sources, no network."""

import asyncio

import pytest

from tradekit.depth.events import BookUpdate, Marker
from tradekit.depth.recorder import AUTO_CHAIN, Recorder, build_sources
from tradekit.depth.sources.das import DASSource


class ListSink:
    def __init__(self):
        self.events = []

    def add(self, event):
        self.events.append(event)

    def flush(self):
        pass

    def markers(self, kind=None, source=None):
        return [
            e
            for e in self.events
            if isinstance(e, Marker) and (kind is None or e.marker == kind) and (source is None or e.source == source)
        ]

    def books(self, source):
        return [e for e in self.events if isinstance(e, BookUpdate) and e.source == source]


class Scripted:
    """A fake source. ``up`` says whether connect() succeeds; ``drop()`` ends its stream."""

    def __init__(self, name, up=True):
        self.name = name
        self.up = up
        self.connects = 0
        self.closed = 0
        self._dropped = asyncio.Event()
        self._tick = 0.005

    async def connect(self):
        self.connects += 1
        if not self.up:
            raise ConnectionError(f"{self.name} down")
        self._dropped = asyncio.Event()

    async def subscribe(self, symbols):
        self.symbols = list(symbols)

    async def events(self):
        while not self._dropped.is_set():
            yield BookUpdate(self.name, "AAPL", "bid", 0, "update", 190.0, 100.0)
            await asyncio.sleep(self._tick)

    def drop(self):
        self.up = False
        self._dropped.set()

    async def close(self):
        self.closed += 1


def run(recorder, script, timeout=3.0):
    """Run ``recorder`` while ``script(stop)`` drives the scenario, then stop it."""

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(recorder.run(stop))
        try:
            await asyncio.wait_for(script(stop), timeout)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout)

    asyncio.run(main())


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# ----- source selection -----


def test_auto_chain_is_das_then_ibkr_and_never_alpaca():
    assert AUTO_CHAIN == ("das", "ibkr")
    made = []
    factories = {n: (lambda n=n: made.append(n) or Scripted(n)) for n in ("das", "ibkr", "alpaca")}
    assert [s.name for s in build_sources("auto", factories)] == ["das", "ibkr"]
    assert "alpaca" not in made


def test_explicit_source_is_exactly_that_source():
    factories = {n: (lambda n=n: Scripted(n)) for n in ("das", "ibkr", "alpaca")}
    assert [s.name for s in build_sources("alpaca", factories)] == ["alpaca"]
    assert [s.name for s in build_sources("ibkr", factories)] == ["ibkr"]
    with pytest.raises(ValueError):
        build_sources("polygon", factories)


# ----- running -----


def test_single_source_records_and_marks_start_and_stop():
    sink, src = ListSink(), Scripted("ibkr")
    rec = Recorder([src], ["aapl"], sink, retry_seconds=0.01)

    async def script(stop):
        await until(lambda: len(sink.books("ibkr")) >= 3)

    run(rec, script)
    assert src.symbols == ["AAPL"]
    assert sink.markers("start", "ibkr") and sink.markers("stop", "ibkr")
    assert src.closed >= 1  # stopping the recorder closes the source


def test_unimplemented_das_falls_back_to_ibkr_and_is_reported_once():
    sink, ibkr = ListSink(), Scripted("ibkr")
    rec = Recorder([DASSource(), ibkr], ["AAPL"], sink, retry_seconds=0.01)

    async def script(stop):
        await until(lambda: len(sink.books("ibkr")) >= 3)
        await asyncio.sleep(0.1)  # several more supervision passes

    run(rec, script)
    not_impl = [m for m in sink.markers("error", "das") if "not implemented" in m.detail]
    assert len(not_impl) == 1
    assert sink.markers("switch", "ibkr")


def test_primary_down_at_start_then_back_switches_with_overlap():
    sink, das, ibkr = ListSink(), Scripted("das", up=False), Scripted("ibkr")
    rec = Recorder([das, ibkr], ["AAPL"], sink, retry_seconds=0.02, overlap_seconds=0.1)

    async def script(stop):
        await until(lambda: len(sink.books("ibkr")) >= 2)
        das.up = True  # DAS comes back
        await until(lambda: sink.markers("stop", "ibkr"))

    run(rec, script)
    start_das = sink.events.index(sink.markers("start", "das")[0])
    stop_ibkr = sink.events.index(sink.markers("stop", "ibkr")[0])
    assert start_das < stop_ibkr
    overlap = sink.events[start_das:stop_ibkr]
    assert any(isinstance(e, BookUpdate) and e.source == "das" for e in overlap)
    assert any(isinstance(e, BookUpdate) and e.source == "ibkr" for e in overlap)  # both ran
    assert any("from ibkr" in m.detail for m in sink.markers("switch", "das"))


def test_primary_drop_fails_over_immediately_not_at_next_retry():
    sink, das, ibkr = ListSink(), Scripted("das"), Scripted("ibkr")
    rec = Recorder([das, ibkr], ["AAPL"], sink, retry_seconds=30.0, overlap_seconds=0.01)

    async def script(stop):
        await until(lambda: len(sink.books("das")) >= 2)
        das.drop()
        # Well inside the 30 s retry interval: failover is driven by the stream ending.
        await until(lambda: len(sink.books("ibkr")) >= 2, timeout=1.0)

    run(rec, script)
    assert sink.markers("stop", "das") and sink.markers("start", "ibkr")


def test_no_source_writes_gap_start_and_gap_end():
    sink, das, ibkr = ListSink(), Scripted("das", up=False), Scripted("ibkr", up=False)
    rec = Recorder([das, ibkr], ["AAPL"], sink, retry_seconds=0.01)

    async def script(stop):
        await until(lambda: sink.markers("gap_start"))
        ibkr.up = True
        await until(lambda: sink.markers("gap_end"))

    run(rec, script)
    assert len(sink.markers("gap_start")) == 1  # one gap, not one per retry
    assert sink.markers("gap_end", "ibkr")


def test_missing_sdk_is_reported_once_then_skipped():
    from tradekit.depth.sources import MissingExtra

    class NoSDK(Scripted):
        async def connect(self):
            self.connects += 1
            raise MissingExtra("IBKR source", "ibkr", "ib_async")

    sink, ibkr = ListSink(), NoSDK("ibkr")
    rec = Recorder([ibkr], ["AAPL"], sink, retry_seconds=0.01)

    async def script(stop):
        await asyncio.sleep(0.15)  # many supervision passes

    run(rec, script)
    assert ibkr.connects == 1
    errors = sink.markers("error", "ibkr")
    assert len(errors) == 1 and "tradekit-cli[ibkr]" in errors[0].detail


def test_repeated_identical_error_is_marked_once_but_retried():
    sink, ibkr = ListSink(), Scripted("ibkr", up=False)
    rec = Recorder([ibkr], ["AAPL"], sink, retry_seconds=0.01)

    async def script(stop):
        await until(lambda: ibkr.connects >= 5)

    run(rec, script)
    assert len(sink.markers("error", "ibkr")) == 1  # same message every pass: one marker


def test_explicit_source_is_retried_never_substituted():
    sink, alpaca = ListSink(), Scripted("alpaca", up=False)
    rec = Recorder([alpaca], ["AAPL"], sink, retry_seconds=0.01)

    async def script(stop):
        await until(lambda: alpaca.connects >= 3)

    run(rec, script)
    assert {e.source for e in sink.events if isinstance(e, BookUpdate)} == set()
    assert sink.markers("gap_start")
