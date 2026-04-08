"""
broadcaster.py — Alpha broadcast client

Called by scanner.process_pair() after a qualifying alert clears the operator's
Telegram dispatch. POSTs a compact alert payload to the license server's
/api/broadcast endpoint, which fans it out to all registered buyers via their
own Telegram bots.

Fire-and-forget from the bot's perspective — failure never blocks the operator's
own alert flow.

New .env vars:
    BROADCAST_ENABLED    = false   # opt-in, off by default
    BROADCAST_MIN_SCORE  = 70      # only broadcast alerts at or above this score
    # These already exist in .env from the registration flow:
    LICENSE_SERVER_URL   = https://keys.breadbot.app:8002
    LICENSE_ADMIN_SECRET = ...
"""

import os
import logging

log = logging.getLogger("broadcaster")

BROADCAST_ENABLED    = os.getenv("BROADCAST_ENABLED",   "false").lower() == "true"
BROADCAST_MIN_SCORE  = int(os.getenv("BROADCAST_MIN_SCORE", "70"))
LICENSE_SERVER_URL   = os.getenv("LICENSE_SERVER_URL",  "").strip().rstrip("/")
LICENSE_ADMIN_SECRET = os.getenv("LICENSE_ADMIN_SECRET","").strip()


async def broadcast_alert(client, pair: dict, score: int, flags: list) -> None:
    """
    POST a qualifying scanner alert to the license server for fan-out to
    all registered buyers.

    Parameters
    ----------
    client : httpx.AsyncClient
        The shared HTTP client from scan_loop — reused to avoid new connections.
    pair   : dict
        Normalised pair dict from process_pair (same fields as build_approval_message).
    score  : int
        Security score 0–100.
    flags  : list[str]
        Deduction flags from check_token_security.
    """
    if not BROADCAST_ENABLED:
        return

    if score < BROADCAST_MIN_SCORE:
        log.debug(
            "broadcast_alert: score %d < threshold %d — skipping %s",
            score, BROADCAST_MIN_SCORE, pair.get("symbol", "?"),
        )
        return

    if not LICENSE_SERVER_URL or not LICENSE_ADMIN_SECRET:
        log.debug(
            "broadcast_alert: LICENSE_SERVER_URL or LICENSE_ADMIN_SECRET not set"
        )
        return

    payload = {
        "symbol":     pair.get("symbol",     "?"),
        "token_name": pair.get("token_name", ""),
        "chain":      pair.get("chain",      ""),
        "token_addr": pair.get("token_addr", ""),
        "price_usd":  float(pair.get("price_usd",  0) or 0),
        "liquidity":  float(pair.get("liquidity",  0) or 0),
        "volume_24h": float(pair.get("volume_24h", 0) or 0),
        "mcap":       float(pair.get("mcap",       0) or 0),
        "score":      score,
        "flags":      list(flags or []),
    }

    try:
        resp = await client.post(
            f"{LICENSE_SERVER_URL}/api/broadcast",
            json=payload,
            headers={"Authorization": f"Bearer {LICENSE_ADMIN_SECRET}"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info(
                "broadcast_alert: %s score=%d sent=%d failed=%d id=%s",
                payload["symbol"], score,
                data.get("sent",   0),
                data.get("failed", 0),
                data.get("broadcast_id", "?"),
            )
        else:
            log.warning(
                "broadcast_alert: server returned %d for %s — %s",
                resp.status_code, payload["symbol"], resp.text[:120],
            )
    except Exception as exc:
        # Never let broadcast failure surface to the caller
        log.warning("broadcast_alert: request failed (non-fatal): %s", exc)
