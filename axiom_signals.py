#!/usr/bin/env python3
"""
axiom_signals.py — Axiom-inspired token signal layer for Breadbot scanner.

Three data sources, all additive on top of existing DEXScreener scan:

1. DEXScreener Boost Feed (no auth, always on)
   Endpoint: api.dexscreener.com/token-boosts/top/v1
   Signal: Boosted = paid promotion = team has capital and conviction.
   Score: +4 if in boost list.

2. Axiom Pump Stream (optional, needs AXIOM_SESSION_COOKIE)
   Endpoints: api8.axiom.trade/pump-live-stream-alerts
              api8.axiom.trade/pump-live-stream-tokens-v2
   Signal: Token appearing in Axiom's curated feed = platform has picked it up.
   Score: +6 if in Axiom alerts within the last 30 min.
   Cookie: Extract from Chrome DevTools (Application > Cookies > api8.axiom.trade)
           Store in Vaultwarden → Breadbot → "Axiom Session Cookie"
           Add to .env as AXIOM_SESSION_COOKIE=<value>

3. New Token Discovery (extends fetch_new_pairs in scanner.py)
   When Axiom stream is active, new pump.fun tokens from the stream are
   injected into the scanner queue before DEXScreener picks them up.
   This gives ~5-minute lead time over the DEXScreener profiles feed.

New .env vars:
    AXIOM_ENABLED=true               # master switch
    AXIOM_SESSION_COOKIE=            # httpOnly cookie from Chrome DevTools
    AXIOM_BOOST_ENABLED=true         # DEXScreener boosts (no auth needed)
    AXIOM_BOOST_SCORE=4              # score addition for boosted tokens
    AXIOM_STREAM_SCORE=6             # score addition for Axiom stream tokens
    AXIOM_STREAM_WINDOW_MINUTES=30   # how long a stream hit stays valid
    AXIOM_POLL_INTERVAL_SECONDS=300  # how often to poll feeds (5 min)
    AXIOM_MIN_MCAP_SOL=5             # min market cap in SOL to queue new tokens
"""

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
AXIOM_ENABLED          = os.getenv("AXIOM_ENABLED", "true").lower() == "true"
AXIOM_SESSION_COOKIE   = os.getenv("AXIOM_SESSION_COOKIE", "").strip()
AXIOM_BOOST_ENABLED    = os.getenv("AXIOM_BOOST_ENABLED", "true").lower() == "true"
AXIOM_BOOST_SCORE      = int(os.getenv("AXIOM_BOOST_SCORE", "4"))
AXIOM_STREAM_SCORE     = int(os.getenv("AXIOM_STREAM_SCORE", "6"))
AXIOM_STREAM_WINDOW    = int(os.getenv("AXIOM_STREAM_WINDOW_MINUTES", "30")) * 60
AXIOM_POLL_INTERVAL    = int(os.getenv("AXIOM_POLL_INTERVAL_SECONDS", "300"))
AXIOM_MIN_MCAP_SOL     = float(os.getenv("AXIOM_MIN_MCAP_SOL", "5"))

DEXSCREENER_BOOSTS_TOP    = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
AXIOM_API_BASE            = "https://api8.axiom.trade"

# ── In-memory caches ──────────────────────────────────────────────────────────
# DEXScreener boost cache: {token_addr_lower: boost_amount}
_boost_cache: dict[str, int] = {}
_boost_updated: float = 0.0

# Axiom stream cache: {token_addr_lower: first_seen_ts}
_axiom_stream_cache: dict[str, float] = {}

# New token queue: tokens discovered via Axiom stream for scanner injection
# Structure: [{token_addr, chain, mcap_sol, symbol, name, created_at, socials}]
_new_token_queue: list[dict] = []
_queue_lock = asyncio.Lock()

# ── DEXScreener Boost Feed ────────────────────────────────────────────────────

async def _poll_dexscreener_boosts(client: httpx.AsyncClient) -> None:
    """Poll both boost endpoints and update the in-memory cache."""
    global _boost_cache, _boost_updated

    addrs: dict[str, int] = {}
    for url in [DEXSCREENER_BOOSTS_TOP, DEXSCREENER_BOOSTS_LATEST]:
        try:
            r = await client.get(url, timeout=10)
            if r.status_code != 200:
                log.warning("DEXScreener boosts HTTP %d for %s", r.status_code, url)
                continue
            items = r.json()
            for item in items:
                if item.get("chainId", "").lower() != "solana":
                    continue
                addr = (item.get("tokenAddress") or "").strip().lower()
                amt  = int(item.get("totalAmount") or item.get("amount") or 0)
                if addr:
                    addrs[addr] = max(addrs.get(addr, 0), amt)
        except Exception as exc:
            log.warning("DEXScreener boost poll error (%s): %s", url, exc)

    _boost_cache  = addrs
    _boost_updated = time.time()
    log.info("axiom_signals: DEXScreener boosts updated — %d Solana tokens", len(addrs))


def get_boost_score(token_addr: str) -> tuple[int, list[str]]:
    """
    Check if a token is in the DEXScreener boost list.
    Returns (score_boost, flags).
    """
    if not AXIOM_BOOST_ENABLED or not _boost_cache:
        return 0, []

    addr_lower = token_addr.lower()
    amt = _boost_cache.get(addr_lower, 0)
    if amt > 0:
        return AXIOM_BOOST_SCORE, [f"+{AXIOM_BOOST_SCORE} DEXScreener boosted ({amt} points)"]
    return 0, []


# ── Axiom Stream Feed ─────────────────────────────────────────────────────────

def _axiom_headers() -> dict:
    """Build headers for Axiom API calls using the session cookie."""
    return {
        "Cookie":     AXIOM_SESSION_COOKIE,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept":     "application/json",
        "Referer":    "https://axiom.trade/",
    }


async def _poll_axiom_stream(client: httpx.AsyncClient) -> None:
    """
    Poll the Axiom pump-live-stream-alerts endpoint.
    Updates _axiom_stream_cache and _new_token_queue.
    No-ops gracefully if cookie is not set or request fails.
    """
    if not AXIOM_SESSION_COOKIE:
        return

    ts = int(time.time() * 1000)
    url = f"{AXIOM_API_BASE}/pump-live-stream-alerts?v={ts}"

    try:
        r = await client.get(url, headers=_axiom_headers(), timeout=15)
        if r.status_code != 200:
            log.warning("axiom_signals: stream HTTP %d (cookie may be stale)", r.status_code)
            return

        alerts = r.json()
        if not isinstance(alerts, list):
            log.warning("axiom_signals: unexpected stream response type: %s", type(alerts))
            return

        now = time.time()
        new_count = 0

        async with _queue_lock:
            for alert in alerts:
                addr = (alert.get("tokenAddress") or "").strip().lower()
                if not addr:
                    continue

                # Update stream cache
                if addr not in _axiom_stream_cache:
                    _axiom_stream_cache[addr] = now
                    new_count += 1

                # Queue for scanner if above min mcap and not already seen
                mcap_sol = float(alert.get("marketCapSol") or 0)
                if mcap_sol >= AXIOM_MIN_MCAP_SOL and addr not in _axiom_stream_cache:
                    _new_token_queue.append({
                        "token_addr": alert.get("tokenAddress", ""),
                        "chain":      "solana",
                        "symbol":     alert.get("tokenTicker", ""),
                        "name":       alert.get("tokenName", ""),
                        "mcap_sol":   mcap_sol,
                        "complete":   alert.get("complete", False),
                        "reply_count": alert.get("replyCount", 0),
                        "twitter":    alert.get("twitter", ""),
                        "telegram":   alert.get("telegram", ""),
                        "website":    alert.get("website", ""),
                        "created_at": alert.get("createdAt", ""),
                        "queued_at":  now,
                    })

        # Prune stale entries from stream cache
        cutoff = now - AXIOM_STREAM_WINDOW
        stale = [k for k, v in _axiom_stream_cache.items() if v < cutoff]
        for k in stale:
            del _axiom_stream_cache[k]

        log.info(
            "axiom_signals: Axiom stream updated — %d alerts, %d new tokens, cache=%d",
            len(alerts), new_count, len(_axiom_stream_cache)
        )

    except Exception as exc:
        log.warning("axiom_signals: Axiom stream poll error: %s", exc)


def get_axiom_stream_score(token_addr: str) -> tuple[int, list[str]]:
    """
    Check if a token appeared in Axiom's live stream within the window.
    Returns (score_boost, flags).
    """
    if not AXIOM_SESSION_COOKIE or not _axiom_stream_cache:
        return 0, []

    addr_lower = token_addr.lower()
    first_seen = _axiom_stream_cache.get(addr_lower)
    if first_seen is None:
        return 0, []

    age_min = int((time.time() - first_seen) / 60)
    return AXIOM_STREAM_SCORE, [f"+{AXIOM_STREAM_SCORE} Axiom stream hit ({age_min}m ago)"]


# ── Combined scorer ───────────────────────────────────────────────────────────

async def get_axiom_score_boost(
    token_addr: str,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[int, list[str]]:
    """
    Combined Axiom signal boost for a token address.
    Checks both DEXScreener boost list and Axiom stream cache.
    Returns (total_boost, flags).
    """
    if not AXIOM_ENABLED:
        return 0, []

    total = 0
    flags: list[str] = []

    boost_score, boost_flags = get_boost_score(token_addr)
    total += boost_score
    flags += boost_flags

    stream_score, stream_flags = get_axiom_stream_score(token_addr)
    total += stream_score
    flags += stream_flags

    return total, flags


# ── New token queue drain ─────────────────────────────────────────────────────

async def drain_new_token_queue() -> list[dict]:
    """
    Pop all queued new tokens for scanner injection.
    Called by scanner's fetch_new_pairs to supplement DEXScreener feed.
    Returns list of token dicts: {token_addr, chain, symbol, name, ...}
    """
    async with _queue_lock:
        items = list(_new_token_queue)
        _new_token_queue.clear()
    return items


# ── Background poll loop ──────────────────────────────────────────────────────

async def axiom_poll_loop() -> None:
    """
    Background coroutine that polls DEXScreener boosts and Axiom stream
    on AXIOM_POLL_INTERVAL_SECONDS cadence.
    Runs indefinitely alongside other scanner tasks.
    """
    if not AXIOM_ENABLED:
        log.info("axiom_signals: disabled (AXIOM_ENABLED=false)")
        return

    log.info(
        "axiom_signals: starting poll loop (interval=%ds, boosts=%s, stream=%s)",
        AXIOM_POLL_INTERVAL,
        "ON" if AXIOM_BOOST_ENABLED else "OFF",
        "ON" if AXIOM_SESSION_COOKIE else "OFF (no cookie)",
    )

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                if AXIOM_BOOST_ENABLED:
                    await _poll_dexscreener_boosts(client)
                if AXIOM_SESSION_COOKIE:
                    await _poll_axiom_stream(client)
            except Exception as exc:
                log.error("axiom_signals: poll loop error: %s", exc)
            await asyncio.sleep(AXIOM_POLL_INTERVAL)


# ── Status summary ────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Return current cache stats for /status or MCP reporting."""
    return {
        "enabled":          AXIOM_ENABLED,
        "boost_enabled":    AXIOM_BOOST_ENABLED,
        "stream_enabled":   bool(AXIOM_SESSION_COOKIE),
        "boost_cache_size": len(_boost_cache),
        "stream_cache_size": len(_axiom_stream_cache),
        "queue_size":       len(_new_token_queue),
        "boost_age_s":      int(time.time() - _boost_updated) if _boost_updated else None,
    }
