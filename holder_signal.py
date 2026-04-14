"""
holder_signal.py — Holder count growth rate signal for the scanner.

Queries Helius for SPL token holder count on Solana. Compares against
previous alerts for the same token_addr to compute a growth rate.

Returns a score adjustment:
  >= 2x growth in <24h   → +5 ("rapid holder growth")
  >= 1.5x growth <24h    → +3 ("healthy holder growth")
  no prior data           → 0 (neutral)
  holder count shrinking  → -2 ("holders declining")

Also stores holder_count on the pair dict for DB persistence.

Usage (from scanner scoring):
    from holder_signal import get_holder_score
    adj, note = await get_holder_score(pair)
    if adj != 0:
        score += adj
        flags.append(note)
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("holder_signal")

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"

# Helius config — reuse the same key already in alt_data_signals
_HELIUS_KEY = ""

def _load_helius_key() -> str:
    global _HELIUS_KEY
    if _HELIUS_KEY:
        return _HELIUS_KEY
    _HELIUS_KEY = os.getenv("HELIUS_API_KEY", "").strip()
    if not _HELIUS_KEY:
        rpc = os.getenv("SOLANA_RPC_URL", "")
        if "api-key=" in rpc:
            _HELIUS_KEY = rpc.split("api-key=")[-1].split("&")[0]
    return _HELIUS_KEY


async def _get_solana_holder_count(token_addr: str) -> int | None:
    """Get holder count for a Solana SPL token via Helius DAS."""
    key = _load_helius_key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Use getTokenAccounts via Helius enhanced RPC
            r = await client.post(
                f"https://mainnet.helius-rpc.com/?api-key={key}",
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccounts",
                    "params": {"mint": token_addr, "limit": 1000},
                },
            )
            if r.status_code != 200:
                return None
            data = r.json()
            result = data.get("result", {})
            # Helius "total" reflects page count, not global total.
            # Use len(token_accounts) + check cursor for pagination.
            accounts = result.get("token_accounts", [])
            if not accounts:
                return 0
            cursor = result.get("cursor")
            count = len(accounts)
            # If cursor exists, there are more pages — fetch up to 3 more
            page = 0
            while cursor and page < 3:
                page += 1
                r2 = await client.post(
                    f"https://mainnet.helius-rpc.com/?api-key={key}",
                    json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccounts",
                        "params": {"mint": token_addr, "limit": 1000, "cursor": cursor},
                    },
                )
                if r2.status_code != 200:
                    break
                r2_data = r2.json()
                r2_result = r2_data.get("result", {})
                r2_accounts = r2_result.get("token_accounts", [])
                count += len(r2_accounts)
                cursor = r2_result.get("cursor")
                if not r2_accounts:
                    break
            return count
    except Exception as exc:
        log.debug("holder count fetch failed for %s: %s", token_addr[:12], exc)
        return None


def _get_previous_holder_count(token_addr: str) -> tuple[int | None, str | None]:
    """Look up the most recent holder_count for this token from meme_alerts."""
    if not DB_PATH.exists():
        return None, None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        row = conn.execute(
            "SELECT holder_count, created_at FROM meme_alerts "
            "WHERE token_addr=? AND holder_count IS NOT NULL AND holder_count > 0 "
            "ORDER BY id DESC LIMIT 1",
            (token_addr,),
        ).fetchone()
        conn.close()
        if row:
            return int(row[0]), row[1]
        return None, None
    except Exception:
        return None, None


async def get_holder_score(pair: dict) -> tuple[int, str]:
    """
    Compute holder growth score adjustment.

    Args:
        pair: scanner pair dict with at least 'token_addr' and 'chain'

    Returns:
        (score_adjustment, flag_string)
        score_adjustment = 0 means no change (neutral or unavailable)
    """
    chain = pair.get("chain", "").lower()
    token_addr = pair.get("token_addr", "")

    if not token_addr:
        return 0, ""

    # Only Solana for now (Base requires different API)
    if chain != "solana":
        return 0, ""

    current_count = await _get_solana_holder_count(token_addr)
    if current_count is None or current_count == 0:
        return 0, ""

    # Store on pair dict for DB persistence
    pair["holder_count"] = current_count

    # Look up previous
    prev_count, prev_ts = _get_previous_holder_count(token_addr)
    if prev_count is None or prev_count == 0:
        # No prior data — just log the count, neutral score
        log.info("holder_signal: %s holders=%d (no prior data)", token_addr[:12], current_count)
        return 0, ""

    # Compute growth
    ratio = current_count / prev_count
    try:
        hours_ago = (datetime.utcnow() - datetime.fromisoformat(prev_ts)).total_seconds() / 3600
    except Exception:
        hours_ago = 24  # default assumption

    if ratio >= 2.0 and hours_ago <= 24:
        adj, note = 5, f"+5 Rapid holder growth {prev_count}→{current_count} ({ratio:.1f}x in {hours_ago:.0f}h)"
    elif ratio >= 1.5 and hours_ago <= 24:
        adj, note = 3, f"+3 Healthy holder growth {prev_count}→{current_count} ({ratio:.1f}x in {hours_ago:.0f}h)"
    elif ratio < 0.8 and hours_ago <= 24:
        adj, note = -2, f"-2 Holders declining {prev_count}→{current_count} ({ratio:.1f}x in {hours_ago:.0f}h)"
    else:
        adj, note = 0, ""

    if adj != 0:
        log.info("holder_signal: %s %s", token_addr[:12], note)

    return adj, note
