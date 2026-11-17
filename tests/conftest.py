"""Suite-wide test isolation."""

import pytest


@pytest.fixture(autouse=True)
def isolated_api_error_log(monkeypatch, tmp_path):
    """Send api-403 events to a per-test file, never the real shared log.

    Tests that feed fake 403s through ``FinvizSession`` call ``api_errors.record``. Without
    this, each run appended five events to ``~/.local/state/api-errors/events.jsonl``, and the
    daily LifeOS reporter filed them as real Finviz throttling on issue #24.
    Tests that set ``API_ERROR_LOG`` themselves still win: their ``setenv`` runs later.
    """
    log = tmp_path / "api-errors" / "events.jsonl"
    monkeypatch.setenv("API_ERROR_LOG", str(log))
    return log
