"""pump_feed.py — Real-time pump.fun token feed via PumpPortal WebSocket.

Connects to wss://pumpportal.fun/api/data and subscribes to:
  - subscribeNewToken: every new token creation on pump.fun
  - subscribeMigration: tokens graduating from bonding curve to Raydium

Tokens are queued for the scanner to evaluate on the next scan cycle,
using the same drain pattern as axiom_signals.py.

No API key needed for creation + migration events (free tier).
"""

import asyncio
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger("pump_feed")

# ── Config ──────────────────────────────────────────────────────────────────

PUMP_WS_URL = "wss://pumpportal.fun/api/data"
PUMP_FEED_ENABLED = os.getenv("PUMP_FEED_ENABLED", "true").lower() in ("true", "1", "yes")

# Minimum initial buy (SOL) to consider a token worth evaluating.
# Filters out zero-effort spam launches with no initial liquidity.
MIN_INITIAL_BUY_SOL = float(os.getenv("PUMP_MIN_INITIAL_BUY_SOL", "0.5"))

# ── Internal state ──────────────────────────────────────────────────────────

_pump_token_queue: list[dict] = []
_pump_seen: set[str] = set()        # dedup within session
_ws_connected: bool = False
_stats = {"created": 0, "migrated": 0, "queued": 0, "filtered": 0, "errors": 0}


# ── Public API (called by scanner.py) ───────────────────────────────────────

async def drain_pump_token_queue() -> list[dict]:
    """Return and clear all queued pump.fun tokens. Called by scanner each cycle."""
    global _pump_token_queue
    items = list(_pump_token_queue)
    _pump_token_queue.clear()
    return items


def get_pump_feed_status() -> dict[str, Any]:
    """Status dict for /status and Telegram /status output."""
    return {
        "enabled": PUMP_FEED_ENABLED,
        "connected": _ws_connected,
        "queue_size": len(_pump_token_queue),
        **_stats,
    }


# ── WebSocket listener ─────────────────────────────────────────────────────

async def _handle_message(raw: str) -> None:
    """Process a single WebSocket message from PumpPortal."""
    global _stats

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    tx_type = data.get("txType", "")

    if tx_type == "create":
        _stats["created"] += 1
        mint = data.get("mint", "")
        if not mint or mint in _pump_seen:
            return

        # Filter: skip tokens with negligible initial buy
        initial_sol = float(data.get("solAmount", 0) or 0)
        if initial_sol < MIN_INITIAL_BUY_SOL:
            _stats["filtered"] += 1
            return

        _pump_seen.add(mint)
        _pump_token_queue.append({
            "token_addr": mint,
            "chain": "solana",
            "source": "pump_feed_create",
            "name": data.get("name", ""),
            "symbol": data.get("symbol", ""),
            "initial_buy_sol": initial_sol,
            "market_cap_sol": float(data.get("marketCapSol", 0) or 0),
            "creator": data.get("traderPublicKey", ""),
            "timestamp": time.time(),
        })
        _stats["queued"] += 1
        log.info(
            "pump_feed: NEW %s (%s) mint=%s initial=%.2f SOL mcap=%.1f SOL",
            data.get("name", "?"), data.get("symbol", "?"),
            mint[:16], initial_sol,
            float(data.get("marketCapSol", 0) or 0),
        )

    elif tx_type == "migrate" or data.get("method") == "migration":
        _stats["migrated"] += 1
        mint = data.get("mint", "") or data.get("token", "")
        if not mint or mint in _pump_seen:
            return

        _pump_seen.add(mint)
        _pump_token_queue.append({
            "token_addr": mint,
            "chain": "solana",
            "source": "pump_feed_migrate",
            "name": data.get("name", ""),
            "symbol": data.get("symbol", ""),
            "timestamp": time.time(),
        })
        _stats["queued"] += 1
        log.info(
            "pump_feed: MIGRATED %s mint=%s (graduated to Raydium)",
            data.get("name", "?"), mint[:16],
        )


async def pump_feed_loop() -> None:
    """Main loop — connect, subscribe, listen, auto-reconnect."""
    global _ws_connected

    if not PUMP_FEED_ENABLED:
        log.info("pump_feed: disabled (PUMP_FEED_ENABLED=false)")
        return

    log.info("pump_feed: starting (url=%s, min_buy=%.1f SOL)", PUMP_WS_URL, MIN_INITIAL_BUY_SOL)

    # Lazy import — websockets may not be installed in all envs
    try:
        import websockets
    except ImportError:
        log.error("pump_feed: websockets package not installed — run: pip install websockets")
        return

    backoff = 1
    while True:
        try:
            async with websockets.connect(PUMP_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                _ws_connected = True
                backoff = 1
                log.info("pump_feed: connected to PumpPortal")

                # Subscribe to new token creations (free)
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                # Subscribe to migrations / graduations (free)
                await ws.send(json.dumps({"method": "subscribeMigration"}))

                log.info("pump_feed: subscribed to newToken + migration")

                async for message in ws:
                    await _handle_message(message)

        except Exception as exc:
            _ws_connected = False
            _stats["errors"] += 1
            log.warning("pump_feed: connection lost (%s) — reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
