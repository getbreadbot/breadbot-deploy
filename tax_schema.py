"""
Tax schema additions (S70 Phase A).

Idempotent migration that:
  • adds `buy_filled_at` to `grid_fills`
  • adds `exit_price` to `funding_positions`
  • creates `funding_income_events` table
  • seeds tax-related `bot_config` defaults
  • creates `tax_export_view` covering closed positions, grid_fills, and
    closed funding_positions

Called from main.py on startup. Safe to run on every boot — every step
checks for existing state before writing.

Phase B (withhold ledger) and Phase C (funding income hook beyond the
event-stream wiring done here) are out of scope.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"


# ── helpers ──────────────────────────────────────────────────────────────────

def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _view_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ── tax_export_view ──────────────────────────────────────────────────────────
#
# Three SELECT branches unioned together:
#   1. closed scanner positions (`positions` table)
#   2. completed grid cycles    (`grid_fills` table)
#   3. closed funding-arb pairs (`funding_positions` table — spot leg)
#
# Holding period:
#   'short' if (close - open) < 365 days
#   'long'  if (close - open) >= 365 days
#   NULL    if open
#
# tx_type values:
#   'spot_trade'              — scanner meme-coin trade
#   'spot_trade_grid'         — completed grid buy/sell cycle
#   'spot_trade_funding_leg'  — spot leg of a funding-arb pair close
#
# Funding income (Schedule 1, not 8949) is emitted from
# funding_income_events directly by the CSV writer — not via this view.
# ─────────────────────────────────────────────────────────────────────────────

_TAX_EXPORT_VIEW_SQL = """
CREATE VIEW tax_export_view AS
-- 1) closed scanner positions
SELECT
    p.id                                                      AS row_id,
    'spot_trade'                                              AS tx_type,
    COALESCE(p.symbol, p.token_name, p.token_addr)            AS asset,
    p.chain                                                   AS chain,
    p.exchange                                                AS venue,
    p.opened_at                                               AS acquired_at,
    p.closed_at                                               AS disposed_at,
    p.entry_price                                             AS entry_price,
    p.exit_price                                              AS exit_price,
    p.quantity                                                AS quantity,
    p.cost_basis_usd                                          AS cost_basis_usd,
    -- Prefer exit_price-derived proceeds. Fall back to (cost_basis + realized_pnl)
    -- when exit_price is NULL — this preserves correctness for legacy closed
    -- positions whose realized_pnl was recorded but exit_price was not captured.
    (CASE
        WHEN p.exit_price IS NOT NULL
            THEN p.exit_price * p.quantity
        WHEN p.realized_pnl_usd IS NOT NULL AND p.cost_basis_usd IS NOT NULL
            THEN p.cost_basis_usd + p.realized_pnl_usd
        ELSE NULL
     END)                                                     AS proceeds_usd,
    p.realized_pnl_usd                                        AS realized_pnl_usd,
    (CASE
        WHEN p.closed_at IS NULL OR p.opened_at IS NULL THEN NULL
        WHEN julianday(p.closed_at) - julianday(p.opened_at) >= 365
            THEN 'long'
        ELSE 'short'
     END)                                                     AS holding_period,
    NULL                                                      AS wash_sale_disallowed
FROM positions p
WHERE p.status LIKE 'closed%'

UNION ALL

-- 2) completed grid cycles (one row per buy-sell pair)
SELECT
    f.id                                                      AS row_id,
    'spot_trade_grid'                                         AS tx_type,
    f.pair                                                    AS asset,
    NULL                                                      AS chain,
    'binance'                                                 AS venue,
    f.buy_filled_at                                           AS acquired_at,
    f.filled_at                                               AS disposed_at,
    f.buy_price                                               AS entry_price,
    f.sell_price                                              AS exit_price,
    f.quantity                                                AS quantity,
    (f.buy_price * f.quantity)                                AS cost_basis_usd,
    (f.sell_price * f.quantity)                               AS proceeds_usd,
    f.net_profit                                              AS realized_pnl_usd,
    (CASE
        WHEN f.buy_filled_at IS NULL THEN NULL
        WHEN julianday(f.filled_at) - julianday(f.buy_filled_at) >= 365
            THEN 'long'
        ELSE 'short'
     END)                                                     AS holding_period,
    NULL                                                      AS wash_sale_disallowed
FROM grid_fills f

UNION ALL

-- 3) spot leg of closed funding-arb pairs
SELECT
    fp.id                                                     AS row_id,
    'spot_trade_funding_leg'                                  AS tx_type,
    fp.pair                                                   AS asset,
    NULL                                                      AS chain,
    'binance'                                                 AS venue,
    fp.opened_at                                              AS acquired_at,
    fp.closed_at                                              AS disposed_at,
    fp.entry_price                                            AS entry_price,
    fp.exit_price                                             AS exit_price,
    fp.quantity                                               AS quantity,
    (fp.entry_price * fp.quantity)                            AS cost_basis_usd,
    (CASE WHEN fp.exit_price IS NOT NULL
          THEN fp.exit_price * fp.quantity
          ELSE NULL END)                                      AS proceeds_usd,
    -- Spot-leg-only realized P&L = (exit - entry) * qty.
    -- The total arb realized_pnl on funding_positions includes funding
    -- collected, which is income-classified separately on Schedule 1.
    (CASE WHEN fp.exit_price IS NOT NULL
          THEN (fp.exit_price - fp.entry_price) * fp.quantity
          ELSE NULL END)                                      AS realized_pnl_usd,
    (CASE
        WHEN fp.closed_at IS NULL OR fp.opened_at IS NULL THEN NULL
        WHEN julianday(fp.closed_at) - julianday(fp.opened_at) >= 365
            THEN 'long'
        ELSE 'short'
     END)                                                     AS holding_period,
    NULL                                                      AS wash_sale_disallowed
FROM funding_positions fp
WHERE fp.status = 'closed'
"""


# ── public entry point ───────────────────────────────────────────────────────

def ensure_tax_schema() -> None:
    """
    Apply all S70 Phase A schema additions. Idempotent.

    Order:
      1. ALTER TABLE additions (grid_fills, funding_positions)
      2. CREATE TABLE funding_income_events
      3. seed bot_config defaults (INSERT OR IGNORE)
      4. CREATE VIEW tax_export_view (drop + recreate to absorb code changes)
    """
    if not DB_PATH.exists():
        log.warning("tax_schema: DB not found at %s — skipping", DB_PATH)
        return

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        # 1a. grid_fills.buy_filled_at
        if _table_exists(conn, "grid_fills") and not _column_exists(
            conn, "grid_fills", "buy_filled_at"
        ):
            conn.execute("ALTER TABLE grid_fills ADD COLUMN buy_filled_at TEXT")
            log.info("tax_schema: added grid_fills.buy_filled_at")

        # 1b. funding_positions.exit_price
        if _table_exists(conn, "funding_positions") and not _column_exists(
            conn, "funding_positions", "exit_price"
        ):
            conn.execute("ALTER TABLE funding_positions ADD COLUMN exit_price REAL")
            log.info("tax_schema: added funding_positions.exit_price")

        # 2. funding_income_events
        if not _table_exists(conn, "funding_income_events"):
            conn.execute(
                """
                CREATE TABLE funding_income_events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id   INTEGER NOT NULL,
                    pair          TEXT    NOT NULL,
                    payment_usd   REAL    NOT NULL,
                    funding_rate  REAL,
                    occurred_at   TEXT    DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "CREATE INDEX idx_funding_income_position "
                "ON funding_income_events(position_id)"
            )
            conn.execute(
                "CREATE INDEX idx_funding_income_occurred "
                "ON funding_income_events(occurred_at)"
            )
            log.info("tax_schema: created funding_income_events table")

        # 3. bot_config defaults — only insert if absent
        if _table_exists(conn, "bot_config"):
            defaults = [
                ("tax_withhold_enabled",     "false"),
                ("tax_withhold_pct",         "0.30"),
                ("tax_cost_basis_method",    "specific_id"),
            ]
            for key, val in defaults:
                conn.execute(
                    "INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)",
                    (key, val),
                )

        # 4. tax_export_view — drop and recreate so code changes propagate
        if _view_exists(conn, "tax_export_view"):
            conn.execute("DROP VIEW tax_export_view")
        conn.execute(_TAX_EXPORT_VIEW_SQL)
        log.info("tax_schema: tax_export_view ready")

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ensure_tax_schema()
    print("tax_schema: ensure_tax_schema() complete")
