"""
Watchlist monitor (S70 P2).

Background loop that re-runs the research checks against every coin in
the `watchlist` table every WATCHLIST_POLL_SECONDS. Updates last_score,
last_price, last_checked_at on each row. Fires a Telegram alert when:

  • new score has dropped by >= alert_score_drop, or
  • price has moved by >= alert_price_pct in either direction

Alerts are rate-limited to one per coin per WATCHLIST_ALERT_COOLDOWN
seconds via the last_alert_at column, so a coin in steady decline
doesn't spam the chat.

The loop is opt-in via WATCHLIST_MONITOR_ENABLED=true. When disabled,
the watchlist table still works (CRUD via the panel) — this just stops
the polling/alert side.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"

POLL_SECONDS    = int(os.getenv("WATCHLIST_POLL_SECONDS", "300"))    # 5 min
ALERT_COOLDOWN  = int(os.getenv("WATCHLIST_ALERT_COOLDOWN", "3600")) # 1 hr


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _is_enabled() -> bool:
    """Read the flag from .env. DB-toggle support is a P3."""
    return os.getenv("WATCHLIST_MONITOR_ENABLED", "false").strip().lower() == "true"


def _list_watched() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM watchlist ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _update_row(
    wl_id: int,
    score: int,
    price: float,
    symbol: Optional[str],
    name: Optional[str],
) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE watchlist
            SET last_score      = ?,
                last_price      = ?,
                last_checked_at = datetime('now'),
                symbol          = COALESCE(?, symbol),
                name            = COALESCE(?, name)
            WHERE id = ?
            """,
            (score, price, symbol, name, wl_id),
        )
        conn.commit()
    finally:
        conn.close()


def _stamp_alert(wl_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE watchlist SET last_alert_at = datetime('now') WHERE id = ?",
            (wl_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── Alert logic ──────────────────────────────────────────────────────────────

def _should_alert(prev: dict, score: int, price: float) -> tuple[bool, str]:
    """Return (should_alert, reason). Reasons are human-readable, used in TG."""
    # Cooldown check first — cheapest skip
    last_alert = prev.get("last_alert_at")
    if last_alert:
        try:
            t = datetime.fromisoformat(last_alert.replace(" ", "T"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - t < timedelta(seconds=ALERT_COOLDOWN):
                return False, "cooldown"
        except Exception:
            pass

    drop_threshold = int(prev.get("alert_score_drop") or 15)
    pct_threshold  = float(prev.get("alert_price_pct") or 0.20)

    last_score = prev.get("last_score")
    last_price = prev.get("last_price")

    if last_score is not None and (last_score - score) >= drop_threshold:
        return True, f"score dropped {last_score} → {score}"

    if last_price and last_price > 0 and price and price > 0:
        change = (price - last_price) / last_price
        if abs(change) >= pct_threshold:
            direction = "up" if change > 0 else "down"
            return True, f"price moved {direction} {abs(change)*100:.1f}% (${last_price:.6g} → ${price:.6g})"

    return False, ""


# ── Telegram ─────────────────────────────────────────────────────────────────

async def _send_telegram(client: httpx.AsyncClient, text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = os.getenv("TELEGRAM_CHAT_ID",   "").strip()
    if not token or not chat:
        log.debug("research_monitor: telegram not configured, skip alert")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        await client.post(
            url,
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        log.warning("research_monitor: telegram send failed: %s", exc)


# ── Main loop ────────────────────────────────────────────────────────────────

async def watchlist_loop() -> None:
    """Run forever. Logs once per loop iteration. Self-contained — no shared
    httpx client with the scanner so a stuck connection here can't take down
    the alert pipeline."""
    log.info(
        "Watchlist monitor started | poll=%ds cooldown=%ds initially_enabled=%s",
        POLL_SECONDS, ALERT_COOLDOWN, _is_enabled(),
    )
    _was_enabled: Optional[bool] = None

    while True:
        # Hot toggle support — re-read flag each tick. Avoids needing a
        # service restart if the buyer flips it in .env.
        enabled = _is_enabled()
        if enabled != _was_enabled:
            log.info("research_monitor: enabled=%s", enabled)
            _was_enabled = enabled
        if not enabled:
            await asyncio.sleep(POLL_SECONDS)
            continue

        rows = _list_watched()
        if not rows:
            await asyncio.sleep(POLL_SECONDS)
            continue

        # Lazy-import the research function from the panel module so the
        # scoring logic stays in ONE place (panel/research_proxy.py).
        # Bot-side modules can't import from panel/, so we import the same
        # function via sys.path injection.
        try:
            import sys
            panel_dir = str(Path(__file__).parent / "panel")
            if panel_dir not in sys.path:
                sys.path.insert(0, panel_dir)
            from research_proxy import _run_research  # type: ignore
        except Exception as exc:
            log.error("research_monitor: failed to import _run_research: %s", exc)
            await asyncio.sleep(POLL_SECONDS)
            continue

        async with httpx.AsyncClient() as client:
            for prev in rows:
                addr = prev["address"]
                try:
                    data = await _run_research(addr)
                except Exception as exc:
                    log.warning("research_monitor: scan failed for %s: %s",
                                addr[:10], exc)
                    continue

                score = int(data.get("rug_score") or 0)
                dex   = data.get("dexscreener") or {}
                price = float(dex.get("price_usd") or 0)
                symbol = dex.get("symbol") or None
                name   = dex.get("name") or None

                _update_row(prev["id"], score, price, symbol, name)

                fire, reason = _should_alert(prev, score, price)
                if not fire:
                    continue

                # Compose alert text
                label = symbol or prev.get("symbol") or addr[:10]
                msg = (
                    f"🔔 <b>Watchlist alert: {label}</b>\n"
                    f"Chain: {data.get('chain', '?').upper()}\n"
                    f"Reason: {reason}\n"
                    f"Score: {score}/100\n"
                    f"Price: ${price:.6g}\n"
                    f"Address: <code>{addr}</code>"
                )
                await _send_telegram(client, msg)
                _stamp_alert(prev["id"])
                log.info(
                    "research_monitor: alert fired for %s (%s) — %s",
                    label, addr[:10], reason,
                )

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(watchlist_loop())
