#!/usr/bin/env python3
"""
main.py — Breadbot unified entry point.

Starts all concurrent loops with asyncio.gather:
  1. scanner.scan_loop          — DEXScreener polling + alert dispatch (5 min)
  2. scanner.telegram_poller    — Telegram callback + command handler (3 sec)
  3. yield_monitor.yield_loop   — 11-platform yield tracker (1 hour)
  4. yield_rebalancer.rebalancer_loop — spread detector + Telegram alerts (1 hour)
  5. grid_engine.grid_loop      — grid fill monitor + boundary check (60 sec)
  6. funding_arb_engine.funding_arb_loop — funding rate arb (1 hour)

All loops are opt-in via .env flags. Disabled loops exit immediately without
consuming resources. The scanner and Telegram poller always run.

On Railway: called by start.sh after dashboard is up.
On VPS:     replace breadbot-scanner.service ExecStart with python3 main.py
            to get all loops in one process, or keep scanner.py running
            independently (scanner still works standalone).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")


# ── DB init ───────────────────────────────────────────────────────────────────

async def _init_db() -> None:
    """
    Ensure all tables exist before any module tries to write.
    Imports each module's ensure_*_table() so this stays in sync
    automatically as new modules are added.
    """
    import sqlite3
    from config import DB_PATH

    # Core tables — inline to avoid circular imports
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        # WAL mode allows concurrent reads while a write is in progress.
        # This eliminates "database is locked" errors from alt_data/yield loops.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meme_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL,
                token_addr TEXT NOT NULL, token_name TEXT, symbol TEXT,
                price_usd REAL, liquidity REAL, volume_24h REAL, mcap REAL,
                rug_score INTEGER, rug_flags TEXT, alert_sent INTEGER DEFAULT 0,
                decision TEXT DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL,
                token_addr TEXT NOT NULL, token_name TEXT, symbol TEXT,
                entry_price REAL NOT NULL, quantity REAL NOT NULL,
                cost_basis_usd REAL NOT NULL, stop_loss_usd REAL,
                take_profit_25 REAL, take_profit_50 REAL,
                status TEXT DEFAULT 'open', exchange TEXT,
                opened_at TEXT DEFAULT (datetime('now')), closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER,
                action TEXT NOT NULL, price_usd REAL, quantity REAL,
                usd_value REAL, fee_usd REAL DEFAULT 0, pnl_usd REAL,
                tx_hash TEXT, exchange TEXT,
                executed_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS yield_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
                asset TEXT NOT NULL, apy REAL NOT NULL, tvl_usd REAL,
                notes TEXT, recorded_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY, realized_pnl REAL DEFAULT 0,
                unrealized_pnl REAL DEFAULT 0, yield_earned REAL DEFAULT 0,
                fees_paid REAL DEFAULT 0, trades_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY, value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
    finally:
        conn.close()

    # Strategy module tables
    from yield_rebalancer  import ensure_rebalance_table
    from pendle_connector  import ensure_pendle_table
    from grid_engine       import ensure_grid_tables
    from funding_arb_engine import ensure_funding_tables

    ensure_rebalance_table()
    ensure_pendle_table()
    ensure_grid_tables()
    ensure_funding_tables()

    from alt_data_signals import ensure_alt_data_table
    ensure_alt_data_table()

    log.info("All DB tables initialised")


# ── Startup banner ────────────────────────────────────────────────────────────

def _log_startup_config() -> None:
    """Log which engines are enabled so the operator can confirm config at a glance."""
    flags = {
        "Yield monitor":        os.getenv("LST_MONITORING_ENABLED",   "true"),
        "Yield rebalancer":     os.getenv("YIELD_REBALANCE_ENABLED",  "false"),
        "Pendle":               os.getenv("PENDLE_ENABLED",           "false"),
        "Grid trading":         os.getenv("GRID_ENABLED",             "false"),
        "Funding arb":          os.getenv("FUNDING_ARB_ENABLED",      "false"),
        "MEV protection (Jito)":      os.getenv("JITO_ENABLED",              "true"),
        "MEV protection (Flashbots)": os.getenv("FLASHBOTS_PROTECT_ENABLED", "true"),
        "Auto-execute":         os.getenv("EXECUTION_MODE",           "manual"),
        "Alt data signals":     os.getenv("ALT_DATA_ENABLED",          "false"),
        "Coinalyze signals":    os.getenv("COINALYZE_ENABLED",  "false"),
        "Helius signals":       os.getenv("HELIUS_ENABLED",     "false"),
    }
    log.info("=" * 60)
    log.info("Breadbot starting")
    log.info("=" * 60)
    for name, val in flags.items():
        status = "ON " if val.lower() in ("true", "auto") else "off"
        log.info("  %-30s %s", name, status)
    log.info("=" * 60)


# ── Loop wrappers ─────────────────────────────────────────────────────────────
# Each wrapper catches import errors gracefully so a broken optional module
# never prevents the scanner from starting.

async def _run_yield_monitor() -> None:
    try:
        from yield_monitor import yield_loop
        await yield_loop()
    except Exception as exc:
        log.error("yield_monitor crashed: %s", exc, exc_info=True)


async def _run_rebalancer() -> None:
    try:
        from yield_rebalancer import rebalancer_loop
        await rebalancer_loop()
    except Exception as exc:
        log.error("yield_rebalancer crashed: %s", exc, exc_info=True)


async def _run_grid(engine) -> None:
    try:
        from grid_engine import grid_loop
        await grid_loop(engine)
    except Exception as exc:
        log.error("grid_engine crashed: %s", exc, exc_info=True)


async def _run_funding_arb(engine) -> None:
    try:
        from funding_arb_engine import funding_arb_loop
        await funding_arb_loop(engine)
    except Exception as exc:
        log.error("funding_arb_engine crashed: %s", exc, exc_info=True)



async def _run_alt_data() -> None:
    try:
        from alt_data_signals import alt_data_loop
        await alt_data_loop()
    except Exception as exc:
        log.error("alt_data_signals crashed: %s", exc, exc_info=True)


async def _run_alpha_monitor() -> None:
    try:
        from social_signals import monitor_alpha_channels
        await monitor_alpha_channels()
    except Exception as exc:
        log.error("social_signals alpha monitor crashed: %s", exc, exc_info=True)


# ── Main entry point ──────────────────────────────────────────────────────────

async def _register_with_license_server() -> None:
    """
    Register this buyer's Telegram bot token and chat ID with the license server.

    Called once on startup. Allows the operator's alpha channel monitor to
    broadcast shared signals to all active buyers.

    Requires:
        LICENSE_KEY            - the buyer's Whop license key
        TELEGRAM_BOT_TOKEN     - the buyer's bot token
        TELEGRAM_CHAT_ID       - the buyer's chat ID
        LICENSE_SERVER_URL     - https://keys.breadbot.app:8002

    Silently skips if any required var is missing.
    Failure does not block startup — registration is best-effort.
    """
    license_key  = os.getenv("LICENSE_KEY",         "").strip()
    bot_token    = os.getenv("TELEGRAM_BOT_TOKEN",  "").strip()
    chat_id      = os.getenv("TELEGRAM_CHAT_ID",    "").strip()
    server_url   = os.getenv("LICENSE_SERVER_URL",  "").strip().rstrip("/")

    if not all([license_key, bot_token, chat_id, server_url]):
        log.debug("_register_with_license_server: missing vars — skipping")
        return

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{server_url}/api/register",
                json={
                    "license_key":          license_key,
                    "telegram_bot_token":   bot_token,
                    "telegram_chat_id":     chat_id,
                },
            )
            if resp.status_code == 200 and resp.json().get("registered"):
                log.info("License server registration: OK")
            else:
                log.warning(
                    "License server registration returned %d: %s",
                    resp.status_code, resp.text[:120],
                )
    except Exception as exc:
        log.warning("License server registration failed (non-fatal): %s", exc)



async def main() -> None:
    # 1. Init DB before anything else writes to it
    await _init_db()

    # 2. Log startup config
    _log_startup_config()

    # 3. Register with license server (non-blocking, best-effort)
    await _register_with_license_server()

    # 3b. Gemini connector smoke test (non-blocking)
    try:
        from gemini_connector import get_account as _gem_acct
        _gem_acct()
        log.info("Gemini connector: auth OK")
    except Exception as _gem_exc:
        log.warning("Gemini connector: not available at startup (%s)", _gem_exc)

    # 4. Import scanner internals (always runs)
    from scanner import scan_loop, telegram_poller

    # 5. Import engine singletons (already instantiated in scanner module
    #    when it was imported above; we reuse them here for the loops)
    from scanner import _grid_engine, _funding_engine

    # 6. Build task list — scanner always included, others conditional
    import httpx

    async with httpx.AsyncClient() as client:
        # Scanner loops (always on)
        tasks = [
            asyncio.create_task(scan_loop(client),       name="scan_loop"),
            asyncio.create_task(telegram_poller(client), name="telegram_poller"),
        ]

        # Yield monitor (always on — no side effects, pure read/log)
        tasks.append(asyncio.create_task(_run_yield_monitor(), name="yield_monitor"))

        # Rebalancer (opt-in)
        tasks.append(asyncio.create_task(_run_rebalancer(), name="yield_rebalancer"))

        # Grid engine loop (opt-in — needs GRID_ENABLED=true)
        tasks.append(asyncio.create_task(
            _run_grid(_grid_engine), name="grid_engine"
        ))

        # Funding arb loop (opt-in — needs FUNDING_ARB_ENABLED=true)
        tasks.append(asyncio.create_task(
            _run_funding_arb(_funding_engine), name="funding_arb"
        ))

        # Alt data signals loop (opt-in — needs ALT_DATA_ENABLED=true)
        tasks.append(asyncio.create_task(_run_alt_data(), name="alt_data"))

        # Alpha channel monitor (opt-in — needs ALPHA_CHANNEL_IDS + TELEGRAM_SESSION_STRING)
        tasks.append(asyncio.create_task(_run_alpha_monitor(), name="alpha_monitor"))

        # Axiom signal poll loop (DEXScreener boosts + optional Axiom stream)
        from axiom_signals import axiom_poll_loop
        tasks.append(asyncio.create_task(axiom_poll_loop(), name="axiom_signals"))

        log.info("All %d tasks started", len(tasks))

        # Run until the first task raises an unhandled exception.
        # scan_loop and telegram_poller are infinite — they only exit on crash.
        # If any task crashes, log it and let Railway/systemd restart the process.
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )

        for task in done:
            if task.exception():
                log.critical(
                    "Task %s raised an exception: %s",
                    task.get_name(), task.exception(), exc_info=task.exception(),
                )

        # Cancel remaining tasks cleanly
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        log.critical("Main loop exited — process will restart via systemd/Railway")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Breadbot stopped by operator (KeyboardInterrupt)")
