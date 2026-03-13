"""
main.py — The scheduler loop. This is the entry point for Breadbot.

Run with: python3 main.py

What it does:
    Every 5 minutes  — scanner polls DEXScreener, scores candidates, risk check, Telegram alert
    Every 1 hour     — yield monitor polls DeFi Llama, saves to DB, alerts on changes > 0.5%
    At midnight      — resets daily P&L and consecutive loss counters
    On startup       — initializes DB, verifies exchange connections, sends Telegram startup message
    On Buy tap       — connector executes the trade, logs to DB, updates risk manager

Stop with: Ctrl+C  (sends a clean shutdown message to Telegram)
"""

import asyncio
import signal
import sys
from datetime import datetime, timezone
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from data.database import init_db, get_db
from scanner.dexscreener import DexScreener
from scanner.rugcheck import RugChecker
from risk.manager import RiskManager
from yields.monitor import YieldMonitor
from notifications.telegram_bot import TelegramController
from exchange.connector import ExchangeConnector

# ── Logging setup ────────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
    colorize=True,
)
logger.add(
    "breadbot.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
)


# ── Global state ─────────────────────────────────────────────────────────────

risk      = RiskManager()
scanner   = DexScreener()
rugcheck  = RugChecker()
yields    = YieldMonitor()
connector = ExchangeConnector()
telegram: TelegramController = None   # assigned after DB is ready


# ── Scanner job (every 5 minutes) ────────────────────────────────────────────

async def run_scanner():
    """Poll DEXScreener, score candidates, send Telegram alerts for anything that passes."""
    logger.info("Scanner — starting scan cycle")

    try:
        candidates = await scanner.get_new_pairs()
        if not candidates:
            logger.debug("Scanner — no new pairs found this cycle")
            return

        logger.info(f"Scanner — {len(candidates)} candidates found, scoring...")

        for token in candidates:
            # Risk check before spending API calls on scoring
            allowed, reason = risk.is_trading_allowed()
            if not allowed:
                logger.warning(f"Scanner — trading not allowed: {reason}")
                break

            # Security score
            score, flags = await rugcheck.score(token)
            if score < 50:
                logger.debug(f"Scanner — {token['symbol']} blocked (score={score})")
                continue

            # Position size
            position_usd = risk.size_position(score)
            if position_usd == 0:
                continue

            # Save alert to DB
            db = await get_db()
            try:
                cursor = await db.execute(
                    """INSERT INTO meme_alerts
                       (symbol, chain, price_usd, liquidity, volume_24h, market_cap,
                        age_hours, security_score, flags, position_size_usd, decision)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        token["symbol"], token["chain"], token["price_usd"],
                        token["liquidity"], token["volume_24h"], token["market_cap"],
                        token["age_hours"], score, ",".join(flags),
                        position_usd, "pending"
                    )
                )
                await db.commit()
                alert_id = cursor.lastrowid
            finally:
                await db.close()

            # Send Telegram alert
            await telegram.send_alert(token, score, flags, position_usd, alert_id)
            logger.info(f"Scanner — alert sent: {token['symbol']} score={score} size=${position_usd}")

    except Exception as e:
        logger.error(f"Scanner job failed: {e}")


# ── Yield monitor job (every hour) ───────────────────────────────────────────

async def run_yield_monitor():
    """Poll DeFi Llama for current stablecoin yields. Alert if any rate changes > 0.5%."""
    logger.info("Yields — polling rates")

    try:
        snapshots = await yields.fetch_all()
        if not snapshots:
            logger.warning("Yields — no data returned from DeFi Llama")
            return

        db = await get_db()
        try:
            # Get previous snapshot for each platform to calculate change
            for snap in snapshots:
                prev = await db.execute_fetchall(
                    """SELECT apy FROM yield_snapshots
                       WHERE platform=? AND asset=?
                       ORDER BY recorded_at DESC LIMIT 1""",
                    (snap["platform"], snap["asset"])
                )

                # Save new snapshot
                await db.execute(
                    """INSERT INTO yield_snapshots (platform, asset, apy, notes)
                       VALUES (?,?,?,?)""",
                    (snap["platform"], snap["asset"], snap["apy"], snap.get("notes", ""))
                )

                # Alert if rate changed meaningfully
                if prev:
                    prev_apy = prev[0]["apy"]
                    change   = abs(snap["apy"] - prev_apy)
                    if change >= config.YIELD_CHANGE_ALERT_PCT:
                        direction = "up" if snap["apy"] > prev_apy else "down"
                        await telegram.send_message(
                            f"Yield alert: *{snap['platform']} {snap['asset']}*\n"
                            f"{prev_apy:.2f}% -> {snap['apy']:.2f}% ({direction} {change:.2f}%)"
                        )

            await db.commit()
        finally:
            await db.close()

        logger.info(f"Yields — saved {len(snapshots)} snapshots")

    except Exception as e:
        logger.error(f"Yield monitor job failed: {e}")


# ── Midnight reset ────────────────────────────────────────────────────────────

async def run_midnight_reset():
    """Reset daily counters and log a daily summary."""
    logger.info("Midnight reset — resetting daily risk counters")

    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO daily_summary (date, total_pnl, trades_count, win_count)
               SELECT DATE('now', '-1 day'),
                      COALESCE(SUM(pnl_usd), 0),
                      COUNT(*),
                      SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
               FROM trades
               WHERE DATE(closed_at) = DATE('now', '-1 day')"""
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Could not write daily summary: {e}")
    finally:
        await db.close()

    risk.reset_daily()
    logger.info("Daily counters reset")


# ── Trade execution (called from Telegram Buy button) ────────────────────────

async def execute_buy(alert_id: int, symbol: str, usd_amount: float, chain: str):
    """
    Called when the user taps BUY in Telegram.
    Executes the trade, records it in the DB, updates the risk manager.

    This function is wired into telegram_bot.py in the _handle_button method.
    """
    logger.info(f"Executing buy: {symbol} ${usd_amount:.2f} (alert #{alert_id})")

    try:
        order = await connector.place_market_buy(f"{symbol}/USD", usd_amount)

        filled_qty   = float(order.get("filled", 0))
        avg_price    = float(order.get("average", 0))
        stop_loss    = round(avg_price * 0.935, 8)    # 6.5% stop loss
        take_profit  = round(avg_price * 1.15, 8)     # 15% target

        # Record position in DB
        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO positions
                   (symbol, chain, entry_price, quantity, cost_basis,
                    stop_loss, take_profit, status, alert_id)
                   VALUES (?,?,?,?,?,?,?,'open',?)""",
                (symbol, chain, avg_price, filled_qty, usd_amount,
                 stop_loss, take_profit, alert_id)
            )
            await db.execute(
                "UPDATE meme_alerts SET decision='buy' WHERE id=?",
                (alert_id,)
            )
            await db.commit()
        finally:
            await db.close()

        risk.open_positions += 1

        await telegram.send_message(
            f"Trade open: *{symbol}*\n"
            f"Entry:  ${avg_price:.6f}\n"
            f"Size:   ${usd_amount:.2f} ({filled_qty:.4f} units)\n"
            f"Stop:   ${stop_loss:.6f} (-6.5%)\n"
            f"Target: ${take_profit:.6f} (+15%)"
        )

    except Exception as e:
        logger.error(f"Trade execution failed for alert #{alert_id}: {e}")
        await telegram.send_message(
            f"Trade failed for *{symbol}*\nError: {e}\nCheck logs."
        )


# ── Startup ───────────────────────────────────────────────────────────────────

async def startup():
    """Initialize everything and send the startup message to Telegram."""
    global telegram

    logger.info("Breadbot starting up...")

    # Initialize database
    await init_db()
    logger.info("Database ready")

    # Initialize Telegram controller
    # Pass execute_buy so the Buy button can trigger trade execution
    telegram = TelegramController(
        risk_manager=risk,
        db_getter=get_db,
        execute_trade=execute_buy,
    )

    # Verify exchange connections
    await connector.connect()
    health = await connector.verify_connectivity()
    if health["errors"]:
        for err in health["errors"]:
            logger.error(err)

    # Run yield monitor immediately on startup
    await run_yield_monitor()

    # Send startup message
    cb_status  = "connected" if health["coinbase"] else "ERROR"
    kr_status  = "connected" if health["kraken"]   else "ERROR"
    usd_bal    = await connector.get_usd_balance()

    await telegram.send_message(
        f"*Breadbot is online*\n\n"
        f"Coinbase:  {cb_status}\n"
        f"Kraken:    {kr_status}\n"
        f"USD bal:   ${usd_bal:,.2f}\n"
        f"Portfolio: ${risk.portfolio_usd:,.2f}\n\n"
        f"Scanner starts in 5 minutes.\n"
        f"Send /status for current state."
    )

    logger.info("Startup complete")


# ── Shutdown ──────────────────────────────────────────────────────────────────

async def shutdown(scheduler: AsyncIOScheduler):
    """Clean shutdown on Ctrl+C."""
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await connector.close()
    if telegram:
        await telegram.send_message("Breadbot is offline. Send Ctrl+C was received.")
    logger.info("Shutdown complete")


# ── Main entry point ──────────────────────────────────────────────────────────

async def main():
    # Run startup sequence
    await startup()

    # Build scheduler
    scheduler = AsyncIOScheduler(timezone="America/New_York")

    # Every 5 minutes — scan for new meme coin opportunities
    scheduler.add_job(
        run_scanner,
        trigger=IntervalTrigger(minutes=5),
        id="scanner",
        max_instances=1,        # never overlap if a scan runs long
        misfire_grace_time=60,
    )

    # Every hour on the hour — yield rate check
    scheduler.add_job(
        run_yield_monitor,
        trigger=CronTrigger(minute=0),
        id="yields",
        max_instances=1,
    )

    # Midnight — reset daily counters
    scheduler.add_job(
        run_midnight_reset,
        trigger=CronTrigger(hour=0, minute=0),
        id="midnight_reset",
    )

    scheduler.start()
    logger.info("Scheduler running. Press Ctrl+C to stop.")

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(shutdown(scheduler))
        )

    # Start Telegram polling (blocks until shutdown)
    await telegram.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
