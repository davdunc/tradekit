"""Parquet sink: Hive-partitioned, readable as one dataset, crash-safe writes (#9)."""

import datetime as dt

import pyarrow.dataset as ds
import pytest

from tradekit.depth.events import BookUpdate, Marker, Trade
from tradekit.depth.sink import ParquetSink

TS = dt.datetime(2026, 11, 19, 14, 31, tzinfo=dt.UTC)


def _book(source="ibkr", symbol="AAPL", position=0, levels=0):
    return BookUpdate(source, symbol, "bid", position, "update", 190.0, 100.0, depth_levels=levels, ts=TS)


def test_partitions_by_kind_date_source_symbol(tmp_path):
    sink = ParquetSink(tmp_path, flush_rows=1000)
    sink.add(_book())
    sink.add(_book(symbol="MSFT"))
    sink.add(Trade("ibkr", "AAPL", 190.1, 50.0, ts=TS))
    sink.add(Marker("start", "ibkr", "AAPL,MSFT", ts=TS))
    sink.close()

    assert list((tmp_path / "book/date=2026-11-19/source=ibkr/symbol=AAPL").glob("part-*.parquet"))
    assert list((tmp_path / "book/date=2026-11-19/source=ibkr/symbol=MSFT").glob("part-*.parquet"))
    assert list((tmp_path / "trades/date=2026-11-19/source=ibkr/symbol=AAPL").glob("part-*.parquet"))
    assert list((tmp_path / "markers/date=2026-11-19/source=ibkr").glob("part-*.parquet"))
    assert not list(tmp_path.rglob("*.tmp"))  # every write was renamed into place
    assert sink.rows_written == 4


def test_reads_back_as_one_dataset(tmp_path):
    sink = ParquetSink(tmp_path, flush_rows=2)
    for i in range(5):
        sink.add(_book(position=i))
    sink.add(_book(source="alpaca", levels=1))
    sink.close()

    table = ds.dataset(tmp_path / "book", partitioning="hive").to_table()
    rows = table.to_pylist()
    assert len(rows) == 6
    assert sorted(r["position"] for r in rows if r["source"] == "ibkr") == [0, 1, 2, 3, 4]
    assert {r["depth_levels"] for r in rows if r["source"] == "alpaca"} == {1}  # level 1 is labelled


def test_flushes_on_row_count(tmp_path):
    sink = ParquetSink(tmp_path, flush_rows=3, flush_seconds=3600)
    for i in range(3):
        sink.add(_book(position=i))
    assert sink.rows_written == 3  # flushed without close()


def test_flushes_on_time(tmp_path):
    now = [0.0]
    sink = ParquetSink(tmp_path, flush_rows=10_000, flush_seconds=5, clock=lambda: now[0])
    sink.add(_book())
    assert sink.rows_written == 0
    now[0] = 6.0
    sink.add(_book(position=1))
    assert sink.rows_written == 2


def _fail_after_partial_write(monkeypatch, where):
    """Make the write (or the rename) fail after a partial temp file exists."""
    from tradekit.depth import sink as sink_mod

    def partial_write(table, path):
        path.write_bytes(b"PAR1 partial")
        if where == "write":
            raise OSError("disk full")

    def failing_replace(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(sink_mod.pq, "write_table", partial_write)
    if where == "replace":
        monkeypatch.setattr(sink_mod.os, "replace", failing_replace)


@pytest.mark.parametrize("where", ["write", "replace"])
def test_failed_write_leaves_no_temp_file_and_raises(tmp_path, monkeypatch, where):
    """A failed write must not leave a stray .tmp file behind (#44 review)."""
    _fail_after_partial_write(monkeypatch, where)
    sink = ParquetSink(tmp_path, flush_rows=1000)
    sink.add(_book())
    with pytest.raises(OSError):
        sink.flush()
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob("*.parquet"))
    assert sink.rows_written == 0
