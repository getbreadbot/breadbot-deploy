"""
Research / Watchlist schema (S70 P2).

Idempotent migration creating the `watchlist` table used by the Research
page on panel.breadbot.app. Watched coins are periodically re-scored by
research_monitor.py and trigger Telegram alerts on score drops or
price moves beyond per-row thresholds.

Called from main.py on startup, alongside the other ensure_*() helpers.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def ensure_research_schema() -> None:
    """Create the watchlist table if missing. Idempotent."""
    if not DB_PATH.exists():
        log.warning("research_schema: DB not found at %s — skipping", DB_PATH)
        return

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        if not _table_exists(conn, "watchlist"):
            conn.execute(
                """
                CREATE TABLE watchlist (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    address           TEXT    NOT NULL,
                    chain             TEXT    NOT NULL,
                    symbol            TEXT,
                    name              TEXT,
                    added_at          TEXT    DEFAULT (datetime('now')),
                    last_score        INTEGER,
                    last_price        REAL,
                    last_checked_at   TEXT,
                    -- Per-row alert thresholds. Defaults give a sensible
                    -- but not noisy alarm: 15-point score drop or 20% price move.
                    alert_score_drop  INTEGER DEFAULT 15,
                    alert_price_pct   REAL    DEFAULT 0.20,
                    -- Timestamp of the last alert sent for this row, used to
                    -- rate-limit duplicate alerts (1/hour per coin).
                    last_alert_at     TEXT,
                    UNIQUE(address, chain)
                )
                """
            )
            conn.execute(
                "CREATE INDEX idx_watchlist_chain "
                "ON watchlist(chain)"
            )
            conn.commit()
            log.info("research_schema: created watchlist table")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ensure_research_schema()
    print("research_schema: ensure_research_schema() complete")
