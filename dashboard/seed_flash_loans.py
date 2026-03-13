"""
dashboard/seed_flash_loans.py — Seed flash_loans table with demo data.
Run: python dashboard/seed_flash_loans.py
"""

import sqlite3
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "cryptobot.db"


def seed():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flash_loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_hash TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            profit_usdc REAL DEFAULT 0,
            gas_cost_eth REAL DEFAULT 0,
            block_number INTEGER,
            executed_at TEXT
        )
    """)

    now = datetime.now(timezone.utc)
    base_block = 27000000
    rows = []

    for i in range(20):
        tx_hash = "0x" + "".join(random.choices("0123456789abcdef", k=64))
        is_success = random.random() < 0.65  # ~65% success rate
        status = "success" if is_success else "reverted"
        profit = round(random.uniform(3, 85), 2) if is_success else 0
        gas_cost = round(random.uniform(0.0001, 0.0008), 6)
        block = base_block + random.randint(0, 50000)
        days_ago = random.uniform(0, 14)
        executed_at = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((tx_hash, status, profit, gas_cost, block, executed_at))

    conn.executemany("""
        INSERT OR IGNORE INTO flash_loans (tx_hash, status, profit_usdc, gas_cost_eth, block_number, executed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM flash_loans").fetchone()[0]
    conn.close()
    print(f"Seeded flash_loans table. Total rows: {count}")


if __name__ == "__main__":
    seed()
