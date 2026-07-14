"""Read tickers from the Falcon screener's SQLite database.

Falcon (~/Projects/falcon/) persists its screening results to ~/.falcon/falcon.db.
We read the most recent runs and surface the tickers as a watchlist for tradekit.

Two access modes:
- `get_top_symbols`: most-frequently-screened symbols over a lookback window
  (good for weekly review — surfaces names that keep showing up)
- `get_recent_run_symbols`: dedup of last N screen runs (good for daily — most current)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".falcon" / "falcon.db"


class FalconDBNotFound(FileNotFoundError):
    """Raised when no falcon.db exists yet (no screens have been run)."""


@dataclass
class FalconReader:
    db_path: Path = DEFAULT_DB_PATH

    def _conn(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FalconDBNotFound(f"No Falcon DB at {self.db_path}. Run `falcon screen ...` to populate it first.")
        return sqlite3.connect(str(self.db_path))

    def get_top_symbols(
        self,
        strategy_name: str | None = None,
        days_back: int = 7,
        limit: int = 20,
    ) -> list[tuple[str, int]]:
        """Most-frequent symbols over the lookback window.

        Returns list of (symbol, appearance_count) tuples.
        """
        cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
        sql = """
            SELECT s.symbol, COUNT(*) as appearances
            FROM screen_results s
            JOIN screen_runs r ON s.run_id = r.id
            WHERE r.executed_at >= ?
        """
        params: list = [cutoff]
        if strategy_name:
            sql += " AND r.strategy_name = ?"
            params.append(strategy_name)
        sql += " GROUP BY s.symbol ORDER BY appearances DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [(row[0], row[1]) for row in cur.fetchall()]

    def get_recent_run_symbols(
        self,
        strategy_name: str | None = None,
        n_runs: int = 3,
        max_symbols: int = 30,
    ) -> list[str]:
        """Deduplicated list of symbols from the most recent N screen runs.

        Preserves first-appearance order across runs (newest run's symbols first).
        """
        sql = "SELECT id FROM screen_runs"
        params: list = []
        if strategy_name:
            sql += " WHERE strategy_name = ?"
            params.append(strategy_name)
        sql += " ORDER BY executed_at DESC LIMIT ?"
        params.append(n_runs)

        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            run_ids = [row[0] for row in cur.fetchall()]
            if not run_ids:
                return []

            placeholders = ",".join("?" * len(run_ids))
            cur.execute(
                f"SELECT symbol, run_id, rank FROM screen_results "
                f"WHERE run_id IN ({placeholders}) ORDER BY run_id DESC, rank ASC",
                run_ids,
            )
            seen: set[str] = set()
            ordered: list[str] = []
            for sym, _rid, _rank in cur.fetchall():
                if sym not in seen:
                    seen.add(sym)
                    ordered.append(sym)
                if len(ordered) >= max_symbols:
                    break
            return ordered

    def get_strategies(self) -> list[str]:
        """List all strategy names that have ever produced screen runs."""
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT strategy_name FROM screen_runs ORDER BY strategy_name")
            return [row[0] for row in cur.fetchall()]
