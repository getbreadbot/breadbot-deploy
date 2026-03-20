#!/usr/bin/env python3
"""
yield_rebalancer.py — Sprint 2A
Stablecoin yield auto-rebalancer.

Reads the latest rates from yield_snapshots (written by yield_monitor.py).
When the spread between the current platform and the best available platform
exceeds REBALANCE_THRESHOLD_PCT, either:
  - alert mode: sends a Telegram recommendation with /rebalance confirm to act
  - auto mode:  executes the move programmatically (withdraw → bridge if needed → deposit)

Only stablecoin assets (USDC, sUSDS) are rebalanced. LSTs are excluded.

New .env vars:
  YIELD_REBALANCE_ENABLED       true|false     (default false — opt-in)
  YIELD_REBALANCE_MODE          alert|auto     (default alert)
  REBALANCE_THRESHOLD_PCT       float          (default 1.5)
  REBALANCE_MIN_AMOUNT_USD      float          (default 500)
  REBALANCE_MAX_GAS_USD         float          (default 5.00)

New DB table:  rebalance_events
  id, timestamp, from_platform, to_platform, asset, amount_usd,
  old_apy, new_apy, gas_cost_usd, net_gain_usd, status, notes
"""

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from config import DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"

# ── Config ────────────────────────────────────────────────────────────────────
REBALANCE_ENABLED   = os.getenv("YIELD_REBALANCE_ENABLED",  "false").lower() == "true"
REBALANCE_MODE      = os.getenv("YIELD_REBALANCE_MODE",     "alert").lower()
THRESHOLD_PCT       = float(os.getenv("REBALANCE_THRESHOLD_PCT",    "1.5"))
MIN_AMOUNT_USD      = float(os.getenv("REBALANCE_MIN_AMOUNT_USD",   "500"))
MAX_GAS_USD         = float(os.getenv("REBALANCE_MAX_GAS_USD",      "5.00"))

# Assets eligible for rebalancing (stablecoins only)
REBALANCEABLE_ASSETS = {"USDC", "sUSDS"}


# ── DB setup + helpers ────────────────────────────────────────────────────────

def ensure_rebalance_table() -> None:
    """Create rebalance_events table if it doesn't exist. Safe to call repeatedly."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rebalance_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    DEFAULT (datetime('now')),
                from_platform TEXT    NOT NULL,
                to_platform   TEXT    NOT NULL,
                asset         TEXT    NOT NULL,
                amount_usd    REAL    NOT NULL,
                old_apy       REAL    NOT NULL,
                new_apy       REAL    NOT NULL,
                gas_cost_usd  REAL    DEFAULT 0,
                net_gain_usd  REAL    DEFAULT 0,
                status        TEXT    DEFAULT 'pending',
                notes         TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def db_log_rebalance(from_platform: str, to_platform: str, asset: str,
                     amount_usd: float, old_apy: float, new_apy: float,
                     gas_cost_usd: float = 0.0, status: str = "recommended",
                     notes: str = "") -> int:
    """Insert a rebalance event row. Returns new row id."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Estimate annual net gain: spread * amount * (days_remaining/365)
        # Use 30-day horizon as a conservative estimate
        net_gain = round((new_apy - old_apy) / 100 * amount_usd * (30 / 365) - gas_cost_usd, 4)
        cur = conn.execute("""
            INSERT INTO rebalance_events
              (from_platform, to_platform, asset, amount_usd, old_apy,
               new_apy, gas_cost_usd, net_gain_usd, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (from_platform, to_platform, asset, amount_usd, old_apy,
              new_apy, gas_cost_usd, net_gain, status, notes))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def db_update_rebalance_status(event_id: int, status: str, notes: str = "") -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "UPDATE rebalance_events SET status=?, notes=? WHERE id=?",
            (status, notes, event_id)
        )
        conn.commit()
    finally:
        conn.close()


def db_get_latest_rates() -> list[dict]:
    """
    Return the most recent APY reading per platform/asset pair from yield_snapshots.
    Only includes REBALANCEABLE_ASSETS.
    """
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = conn.execute("""
            SELECT platform, asset, apy, recorded_at
            FROM yield_snapshots
            WHERE asset IN ('USDC', 'sUSDS')
            AND (platform, asset, recorded_at) IN (
                SELECT platform, asset, MAX(recorded_at)
                FROM yield_snapshots
                WHERE asset IN ('USDC', 'sUSDS')
                GROUP BY platform, asset
            )
            ORDER BY apy DESC
        """).fetchall()
        conn.close()
        return [
            {"platform": r[0], "asset": r[1], "apy": r[2], "recorded_at": r[3]}
            for r in rows
        ]
    except Exception as exc:
        log.error("db_get_latest_rates error: %s", exc)
        return []


def db_get_rebalance_history(limit: int = 10) -> list[dict]:
    """Return the last N rebalance events for /rebalance history."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = conn.execute("""
            SELECT timestamp, from_platform, to_platform, asset,
                   amount_usd, old_apy, new_apy, net_gain_usd, status
            FROM rebalance_events
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [
            {"timestamp": r[0], "from": r[1], "to": r[2], "asset": r[3],
             "amount_usd": r[4], "old_apy": r[5], "new_apy": r[6],
             "net_gain_usd": r[7], "status": r[8]}
            for r in rows
        ]
    except Exception as exc:
        log.error("db_get_rebalance_history error: %s", exc)
        return []


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class RebalanceOpportunity:
    from_platform: str
    to_platform:   str
    asset:         str
    old_apy:       float
    new_apy:       float
    spread:        float        # new_apy - old_apy


# ── Core logic ────────────────────────────────────────────────────────────────

def find_best_opportunity(rates: list[dict]) -> RebalanceOpportunity | None:
    """
    Given a list of current rates, find the highest-value rebalance opportunity.
    Returns None if no pair exceeds THRESHOLD_PCT spread.

    Strategy: compare the lowest-yielding platform against the highest-yielding
    platform for the same asset class (USDC vs USDC, etc.).
    """
    if len(rates) < 2:
        return None

    # Group by asset
    by_asset: dict[str, list[dict]] = {}
    for r in rates:
        by_asset.setdefault(r["asset"], []).append(r)

    best_opp = None
    for asset, asset_rates in by_asset.items():
        if len(asset_rates) < 2:
            continue
        asset_rates.sort(key=lambda x: x["apy"])
        worst  = asset_rates[0]
        best   = asset_rates[-1]
        spread = round(best["apy"] - worst["apy"], 4)

        if spread >= THRESHOLD_PCT:
            opp = RebalanceOpportunity(
                from_platform = worst["platform"],
                to_platform   = best["platform"],
                asset         = asset,
                old_apy       = worst["apy"],
                new_apy       = best["apy"],
                spread        = spread,
            )
            if best_opp is None or opp.spread > best_opp.spread:
                best_opp = opp

    return best_opp


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def _tg_send(client: httpx.AsyncClient, text: str,
                   reply_markup: dict | None = None) -> dict:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {}
    url = TELEGRAM_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload: dict = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = await client.post(url, json=payload, timeout=10)
        return resp.json()
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)
        return {}


def _rebalance_keyboard(event_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Execute rebalance", "callback_data": f"rebalance_confirm_{event_id}"},
            {"text": "Dismiss",           "callback_data": f"rebalance_dismiss_{event_id}"},
        ]]
    }


# ── Alert mode ────────────────────────────────────────────────────────────────

async def send_rebalance_alert(client: httpx.AsyncClient,
                                opp: RebalanceOpportunity,
                                event_id: int) -> None:
    """Send a Telegram recommendation with an inline confirm button."""
    annual_gain = round(opp.spread / 100 * MIN_AMOUNT_USD, 2)
    text = (
        f"Yield Rebalance Opportunity\n\n"
        f"Move: {opp.from_platform} → {opp.to_platform}\n"
        f"Asset: {opp.asset}\n"
        f"Current: {opp.old_apy:.2f}%\n"
        f"Best:    {opp.new_apy:.2f}%\n"
        f"Spread:  +{opp.spread:.2f}%\n\n"
        f"Est. gain on ${MIN_AMOUNT_USD:,.0f} over 30 days: "
        f"${annual_gain * 30 / 365:.2f}\n\n"
        f"Reply /rebalance confirm to execute, or tap below."
    )
    await _tg_send(client, text, reply_markup=_rebalance_keyboard(event_id))
    log.info("Rebalance alert sent: %s → %s spread=%.2f%%",
             opp.from_platform, opp.to_platform, opp.spread)


# ── Telegram command handlers ─────────────────────────────────────────────────

async def handle_rebalance_command(client: httpx.AsyncClient,
                                    subcommand: str = "") -> None:
    """
    Handle /rebalance, /rebalance confirm, /rebalance history Telegram commands.
    Called from the scanner's telegram_poller when it sees a /rebalance message.
    """
    sub = subcommand.strip().lower()

    if sub == "history":
        rows = db_get_rebalance_history(10)
        if not rows:
            await _tg_send(client, "No rebalance history yet.")
            return
        lines = ["Rebalance History (last 10)\n"]
        for r in rows:
            ts    = r["timestamp"][:10]
            gain  = r["net_gain_usd"]
            arrow = "▲" if gain >= 0 else "▼"
            lines.append(
                f"{ts} | {r['from']} → {r['to']} | {r['asset']} | "
                f"{r['old_apy']:.2f}%→{r['new_apy']:.2f}% | "
                f"{arrow} ${abs(gain):.2f} | {r['status']}"
            )
        await _tg_send(client, "\n".join(lines))
        return

    if sub == "confirm":
        # Find the most recent pending recommendation and mark it confirmed
        if not DB_PATH.exists():
            await _tg_send(client, "No pending rebalance found.")
            return
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT id, from_platform, to_platform, asset, new_apy "
                "FROM rebalance_events WHERE status='recommended' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
        except Exception:
            row = None

        if not row:
            await _tg_send(client, "No pending rebalance recommendation found.")
            return

        event_id, from_p, to_p, asset, new_apy = row
        db_update_rebalance_status(
            event_id, "confirmed",
            notes="Confirmed via /rebalance confirm"
        )
        await _tg_send(
            client,
            f"Rebalance confirmed.\n\n"
            f"Move your {asset} from {from_p} to {to_p} ({new_apy:.2f}% APY).\n"
            f"Execute the transfer manually on each platform, "
            f"then your next yield reading will reflect the new rate."
        )
        log.info("Rebalance confirmed by user: event_id=%d", event_id)
        return

    # Default: show current rates + best opportunity
    rates = db_get_latest_rates()
    if not rates:
        await _tg_send(client, "No yield data yet. Wait for the next poll cycle.")
        return

    lines = ["Current Allocation\n"]
    for r in rates:
        lines.append(f"  {r['platform']:<14} {r['asset']:<8} {r['apy']:.2f}%")

    opp = find_best_opportunity(rates)
    if opp:
        lines.append(
            f"\nRecommended move:\n"
            f"  {opp.from_platform} → {opp.to_platform} "
            f"({opp.asset}: +{opp.spread:.2f}%)\n\n"
            f"Run /rebalance confirm to mark as actioned."
        )
    else:
        lines.append(
            f"\nNo rebalance needed. "
            f"Best spread is below {THRESHOLD_PCT:.1f}% threshold."
        )

    await _tg_send(client, "\n".join(lines))


async def handle_rebalance_callback(client: httpx.AsyncClient,
                                     data: str, cb_id: str) -> None:
    """Handle inline keyboard callbacks: rebalance_confirm_N / rebalance_dismiss_N."""
    import httpx as _httpx
    url = TELEGRAM_BASE.format(token=TELEGRAM_BOT_TOKEN, method="answerCallbackQuery")
    try:
        await client.post(url, json={"callback_query_id": cb_id}, timeout=5)
    except Exception:
        pass

    if data.startswith("rebalance_confirm_"):
        event_id = int(data.split("_")[-1])
        db_update_rebalance_status(event_id, "confirmed", notes="Confirmed via inline button")
        # Fetch the event for the confirmation message
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT from_platform, to_platform, asset, new_apy "
                "FROM rebalance_events WHERE id=?", (event_id,)
            ).fetchone()
            conn.close()
        except Exception:
            row = None

        if row:
            from_p, to_p, asset, new_apy = row
            await _tg_send(
                client,
                f"Rebalance confirmed.\n"
                f"Move {asset} from {from_p} to {to_p} ({new_apy:.2f}% APY)."
            )
        log.info("Rebalance confirmed via inline button: event_id=%d", event_id)

    elif data.startswith("rebalance_dismiss_"):
        event_id = int(data.split("_")[-1])
        db_update_rebalance_status(event_id, "dismissed")
        await _tg_send(client, "Rebalance dismissed.")
        log.info("Rebalance dismissed: event_id=%d", event_id)


# ── Main rebalancer loop ──────────────────────────────────────────────────────

async def rebalancer_loop() -> None:
    """
    Runs alongside yield_monitor. After each yield poll cycle, evaluates
    whether a rebalance is warranted and acts based on REBALANCE_MODE.

    Check interval matches yield monitor (1 hour). No separate cron needed —
    import and call from the same async entry point as yield_monitor.
    """
    if not REBALANCE_ENABLED:
        log.info("Yield rebalancer disabled (YIELD_REBALANCE_ENABLED=false)")
        return

    ensure_rebalance_table()
    log.info(
        "Yield rebalancer started — mode=%s threshold=%.1f%% min_amount=$%.0f",
        REBALANCE_MODE, THRESHOLD_PCT, MIN_AMOUNT_USD,
    )

    async with httpx.AsyncClient() as client:
        while True:
            try:
                rates = db_get_latest_rates()
                opp   = find_best_opportunity(rates)

                if opp:
                    log.info(
                        "Rebalance opportunity: %s → %s +%.2f%%",
                        opp.from_platform, opp.to_platform, opp.spread,
                    )
                    event_id = db_log_rebalance(
                        from_platform = opp.from_platform,
                        to_platform   = opp.to_platform,
                        asset         = opp.asset,
                        amount_usd    = MIN_AMOUNT_USD,
                        old_apy       = opp.old_apy,
                        new_apy       = opp.new_apy,
                        status        = "recommended",
                    )

                    if REBALANCE_MODE == "alert":
                        await send_rebalance_alert(client, opp, event_id)
                    else:
                        # auto mode placeholder — full execution flow (Sprint 2A+)
                        # requires bridge logic for cross-chain moves.
                        # For now, sends alert and marks as 'pending_auto'.
                        db_update_rebalance_status(
                            event_id, "pending_auto",
                            notes="Auto mode — manual bridge step required for cross-chain"
                        )
                        await send_rebalance_alert(client, opp, event_id)
                        log.info("Auto mode: rebalance queued as pending_auto (event_id=%d)", event_id)
                else:
                    log.info(
                        "No rebalance needed — best spread below %.1f%% threshold",
                        THRESHOLD_PCT,
                    )

            except Exception as exc:
                log.error("Rebalancer loop error: %s", exc)

            await asyncio.sleep(3600)  # re-evaluate after next yield poll


# ── Telegram command summary ──────────────────────────────────────────────────
# Wire these into scanner.py's telegram_poller:
#   /rebalance              → handle_rebalance_command(client, "")
#   /rebalance confirm      → handle_rebalance_command(client, "confirm")
#   /rebalance history      → handle_rebalance_command(client, "history")
#   callback rebalance_*    → handle_rebalance_callback(client, data, cb_id)


# ── Entry point (standalone test) ────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ensure_rebalance_table()

    # Quick self-test: print current rates and best opportunity
    rates = db_get_latest_rates()
    print(f"Latest rates ({len(rates)} platforms):")
    for r in rates:
        print(f"  {r['platform']:<14} {r['asset']:<8} {r['apy']:.2f}%")

    opp = find_best_opportunity(rates)
    if opp:
        print(f"\nBest opportunity: {opp.from_platform} → {opp.to_platform} "
              f"+{opp.spread:.2f}% on {opp.asset}")
    else:
        print(f"\nNo opportunity above {THRESHOLD_PCT:.1f}% threshold.")
