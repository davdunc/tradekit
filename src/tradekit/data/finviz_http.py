"""One HTTP path for every Finviz request: shared rate limit + incremental backoff.

Finviz allows roughly 10 requests per 5 seconds, plus an hourly cap, and both budgets are
shared by every consumer authenticating as the account — tradekit, ``premarket_rvol.py``,
falcon-core, on every node. A throttled request comes back as **403 Forbidden** (sometimes
429), and on CSV endpoints sometimes as a 200 carrying an HTML page instead of CSV.

Before this module existed each call site was a bare ``requests.get``: no spacing, no retry,
and a throttled response turned into "no candidates found". Now:

- ``RateLimiter`` spaces requests to ``max_requests`` per ``window`` seconds. The timestamps
  live in a file under ``state_dir()`` behind an ``fcntl`` lock, so two tradekit processes on
  one node (``tradekit scan`` and ``tradekit second-day`` started together) share one budget.
- ``FinvizSession.request`` retries 403 / 429 / 5xx / connection errors with an
  exponentially growing, capped, jittered delay, honouring ``Retry-After`` when present.
- When retries run out it raises ``FinvizThrottled`` — callers must not read that as
  "the screen returned nothing".

The hourly cap will not clear inside one backoff ladder (about 30 s total by default), so the
real fix for volume is fetching less often: see ``FinvizEliteProvider.get_universe``.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import requests

from tradekit.data import api_errors

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})

try:  # POSIX only; tradekit runs on Linux / WSL, but don't break an import elsewhere.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class FinvizThrottled(RuntimeError):
    """Finviz kept refusing (403/429/5xx or HTML-for-CSV) after every retry."""

    def __init__(self, url: str, attempts: int, last: str):
        super().__init__(f"Finviz throttled after {attempts} attempts ({last}): {redact(url)}")
        self.url = redact(url)
        self.attempts = attempts
        self.last = last


@dataclass(frozen=True)
class BackoffPolicy:
    """Delay before retry *n* (1-based) = min(cap, base * factor**(n-1)), plus up to ``jitter`` × that."""

    max_attempts: int = 5
    base: float = 2.0
    factor: float = 2.0
    cap: float = 60.0
    jitter: float = 0.25

    def delay(self, retry: int, rng: Callable[[], float] = random.random) -> float:
        d = min(self.cap, self.base * self.factor ** (retry - 1))
        return min(self.cap, d * (1.0 + self.jitter * rng()))


_AUTH_RE = re.compile(r"(auth=)[^&]+")


def redact(url: str) -> str:
    """Strip the Elite token from a URL before it reaches a log line or exception."""
    return _AUTH_RE.sub(r"\1***", url)


def _retry_after(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; not worth parsing for Finviz


def looks_like_html(resp: requests.Response) -> bool:
    """A CSV endpoint that answers with a web page is a throttle/login page, not data."""
    ctype = resp.headers.get("Content-Type", "").lower()
    if "html" in ctype:
        return True
    head = resp.text[:512].lstrip().lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


class RateLimiter:
    """At most ``max_requests`` per ``window`` seconds, shared across processes via a lock file."""

    def __init__(
        self,
        max_requests: int = 10,
        window: float = 5.0,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.max_requests = max_requests
        self.window = window
        self._path = path
        self._clock = clock
        self._sleep = sleep
        self._local = threading.Lock()
        self._mem: list[float] = []

    @property
    def path(self) -> Path:
        if self._path is None:
            from tradekit.paths import state_dir

            self._path = state_dir() / "finviz_ratelimit.json"
        return self._path

    @contextmanager
    def _locked(self):
        with self._local:
            if fcntl is None:
                yield None
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield fh
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _load(self) -> list[float]:
        if fcntl is None:
            return list(self._mem)
        try:
            return [float(t) for t in json.loads(self.path.read_text())]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self, stamps: list[float]) -> None:
        if fcntl is None:
            self._mem = stamps
            return
        self.path.write_text(json.dumps(stamps))

    def acquire(self) -> float:
        """Block until a request slot is free, record it, and return seconds waited."""
        waited = 0.0
        with self._locked():
            while True:
                now = self._clock()
                stamps = [t for t in self._load() if now - t < self.window]
                if len(stamps) < self.max_requests:
                    stamps.append(now)
                    self._save(stamps)
                    return waited
                wait = self.window - (now - min(stamps)) + 0.01
                waited += wait
                self._sleep(wait)


_DEFAULT_LIMITER = RateLimiter()


class FinvizSession(requests.Session):
    """``requests.Session`` whose every request is rate-limited and retried with backoff."""

    def __init__(
        self,
        policy: BackoffPolicy | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__()
        self.policy = policy or BackoffPolicy()
        self.limiter = limiter or _DEFAULT_LIMITER
        self._sleep = sleep

    def request(self, method, url, *args, expect_csv: bool = False, **kwargs):  # type: ignore[override]
        last = ""
        for attempt in range(1, self.policy.max_attempts + 1):
            self.limiter.acquire()
            delay: float | None = None
            try:
                resp = super().request(method, url, *args, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = type(e).__name__
            else:
                if resp.status_code in RETRYABLE_STATUS:
                    last = f"HTTP {resp.status_code}"
                    delay = _retry_after(resp)
                    if resp.status_code == 403:
                        api_errors.record("finviz", url, 403, detail=f"attempt {attempt}/{self.policy.max_attempts}")
                elif expect_csv and resp.ok and looks_like_html(resp):
                    last = "HTML instead of CSV"
                else:
                    return resp
            if attempt == self.policy.max_attempts:
                break
            if delay is None:
                delay = self.policy.delay(attempt)
            delay = min(delay, self.policy.cap)
            logger.warning(
                "Finviz %s on %s — retry %d/%d in %.1fs",
                last,
                redact(url),
                attempt,
                self.policy.max_attempts - 1,
                delay,
            )
            self._sleep(delay)
        raise FinvizThrottled(url, self.policy.max_attempts, last)


_SESSION: FinvizSession | None = None


def session() -> FinvizSession:
    """The process-wide Finviz session (shares one limiter with every other caller)."""
    global _SESSION
    if _SESSION is None:
        _SESSION = FinvizSession()
    return _SESSION


def finviz_get(url: str, *, expect_csv: bool = False, timeout: float = 15, **kwargs) -> requests.Response:
    """GET through the shared session; raises ``FinvizThrottled`` or ``HTTPError``, never returns a 4xx/5xx."""
    resp = session().get(url, expect_csv=expect_csv, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def install_on_vendored_scraper() -> None:
    """Route the vendored ``finvizfinance`` scraper through the shared session.

    Its ``web_scrap`` reads the module-global ``finvizfinance.util.session``; replacing that one
    object puts the public-site fallback under the same limiter and backoff without editing
    vendored source.
    """
    try:
        import finvizfinance.util as fv_util
    except ImportError:  # pragma: no cover
        return
    if not isinstance(fv_util.session, FinvizSession):
        fv_util.session = session()
