"""Exclusive inter-process file lock that works on POSIX (``fcntl``) and Windows (``msvcrt``).

Kept dependency-free on purpose: the Fedora RPM build can't pull ``filelock`` from PyPI.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":  # pragma: no cover - exercised on Windows CI
    import msvcrt

    def _lock(fh) -> None:
        # LK_LOCK gives up after ~10 s and raises; keep waiting like flock(LOCK_EX) does.
        while True:
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (created if missing) for the ``with`` block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" never truncates and creates the file; Windows locks byte 0 even on an empty file.
    with open(path, "a+", encoding="utf-8") as fh:
        _lock(fh)
        try:
            yield
        finally:
            _unlock(fh)
