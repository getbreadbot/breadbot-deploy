#!/usr/bin/env python3
"""
social_signals.py — Social signal layer for Breadbot scanner.

Two signal sources:
  1. Arkham Intelligence API — checks whether labeled smart-money wallets
     have bought into a token within the last 6 hours.
  2. Telethon alpha channel monitor — listens to configured Telegram alpha
     channels for contract addresses; flags tokens that appear in 2+ channels
     within a 15-minute window.

Scanner integration:
    from social_signals import get_social_score_boost, handle_alpha_command
    boost, flags = get_social_score_boost(token_addr, chain)
    # boost is an int (positive = add to score)
    # flags is a list of human-readable strings to append to alert flags

New .env vars:
    ARKHAM_API_KEY=           free at arkhamintelligence.com
    ALPHA_CHANNEL_IDS=        comma-separated Telegram channel IDs to monitor
    ALPHA_SIGNAL_BOOST=8      score addition per multi-channel confirmation
    SMART_MONEY_SIGNAL_BOOST=8 score addition for known wallet activity
    TELEGRAM_SESSION_STRING=  Telethon session string (generate once, reuse)
"""

import asyncio
import logging
import os
import re
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

ARKHAM_API_KEY         = os.getenv("ARKHAM_API_KEY", "").strip()
ALPHA_CHANNEL_IDS_RAW  = os.getenv("ALPHA_CHANNEL_IDS", "").strip()
ALPHA_CHANNEL_IDS      = [c.strip() for c in ALPHA_CHANNEL_IDS_RAW.split(",") if c.strip()]
ALPHA_SIGNAL_BOOST     = int(os.getenv("ALPHA_SIGNAL_BOOST", "8"))
SMART_MONEY_BOOST      = int(os.getenv("SMART_MONEY_SIGNAL_BOOST", "8"))
TELEGRAM_SESSION       = os.getenv("TELEGRAM_SESSION_STRING", "").strip()

ARKHAM_BASE            = "https://api.arkhamintelligence.com"
ALPHA_WINDOW_SECONDS   = 900   # 15-minute window for multi-channel detection
ALPHA_MIN_CHANNELS     = 2     # minimum channels to trigger boost
ARKHAM_LOOKBACK_HOURS  = 6     # hours of Arkham history to check

# ── In-memory alpha channel hit store ────────────────────────────────────────
# Structure: { token_addr_lower: [(channel_id, timestamp), ...] }
_alpha_hits: dict[str, list[tuple[str, float]]] = defaultdict(list)
_alpha_lock = asyncio.Lock()

# ── Solana + EVM contract address patterns ────────────────────────────────────
_SOL_ADDR_RE  = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
_EVM_ADDR_RE  = re.compile(r'\b0x[0-9a-fA-F]{40}\b')

# ── Arkham Intelligence ───────────────────────────────────────────────────────

async def get_arkham_wallet_activity(
    token_addr: str,
    chain: str,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[int, list[str]]:
    """
    Query Arkham for labeled smart-money wallet activity on this token.
    Returns (score_boost, flags_list).
    Returns (0, []) immediately if ARKHAM_API_KEY is not set.
    """
    if not ARKHAM_API_KEY:
        return 0, []

    boost = 0
    flags: list[str] = []

    headers = {"API-Key": ARKHAM_API_KEY}
    # Arkham entity search: find token transfers involving labeled entities
    # We look at the token's recent transfers and check if any sender/receiver
    # is a labeled smart-money entity.
    url = f"{ARKHAM_BASE}/token/{token_addr}/transfers"
    params = {
        "chain":  "solana" if chain == "solana" else "base",
        "limit":  50,
        "usdGte": 1000,   # only transfers above $1k
    }

    # Cutoff: only look at last ARKHAM_LOOKBACK_HOURS hours
    cutoff_ts = int(time.time()) - ARKHAM_LOOKBACK_HOURS * 3600

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=10)

    try:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 401:
            log.warning("Arkham: invalid API key")
            return 0, []
        if resp.status_code == 404:
            # Token not indexed yet — common for new tokens
            return 0, []
        if resp.status_code != 200:
            log.warning("Arkham: HTTP %d for %s", resp.status_code, token_addr[:12])
            return 0, []

        data = resp.json()
        transfers = data.get("transfers", []) or []

        smart_money_wallets: set[str] = set()
        whale_holders: list[tuple[str, float]] = []  # (label, pct)

        for tx in transfers:
            ts = tx.get("blockTimestamp", 0) or 0
            if ts < cutoff_ts:
                continue

            # Check both from and to addresses for labels
            for direction in ("fromAddress", "toAddress"):
                addr_info = tx.get(direction) or {}
                entity    = addr_info.get("arkhamEntity") or {}
                label     = entity.get("name", "")
                entity_type = entity.get("type", "").lower()

                if not label:
                    continue

                # Smart money = labeled non-exchange wallet that bought in
                if direction == "toAddress" and entity_type not in ("exchange", "cex", "dex"):
                    smart_money_wallets.add(label)

                # Whale = any labeled entity holding significant supply
                pct = float(tx.get("toAddressHolderPct") or 0)
                if pct > 0.05 and label:
                    whale_holders.append((label, pct))

        if smart_money_wallets:
            boost += SMART_MONEY_BOOST
            wallets_str = ", ".join(list(smart_money_wallets)[:3])
            flags.append(f"+{SMART_MONEY_BOOST} Smart money: {wallets_str}")
            log.info("Arkham smart money detected for %s: %s", token_addr[:12], wallets_str)

        for label, pct in whale_holders[:2]:
            flags.append(f"+5 Whale holder ({label} {pct*100:.0f}%)")
            boost += 5

    except Exception as exc:
        log.warning("Arkham lookup failed for %s: %s", token_addr[:12], exc)
    finally:
        if own_client:
            await client.aclose()

    return boost, flags

# ── Alpha channel monitor (Telethon) ─────────────────────────────────────────

async def monitor_alpha_channels() -> None:
    """
    Background coroutine that connects to Telegram as a user account via Telethon
    and listens for contract addresses in configured alpha channels.

    Requires TELEGRAM_SESSION_STRING and ALPHA_CHANNEL_IDS in .env.
    Exits immediately (no-op) if either is not set.

    To generate TELEGRAM_SESSION_STRING:
        pip install telethon
        python3 -c "
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        import os
        client = TelegramClient(StringSession(), int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
        client.start()
        print(client.session.save())
        "
    Store the output string in .env as TELEGRAM_SESSION_STRING.
    TG_API_ID and TG_API_HASH come from https://my.telegram.org/apps
    """
    if not TELEGRAM_SESSION or not ALPHA_CHANNEL_IDS:
        log.info("social_signals: alpha channel monitor disabled (session or channels not set)")
        return

    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except ImportError:
        log.warning("social_signals: telethon not installed — alpha channel monitor disabled")
        log.warning("Install with: pip install telethon")
        return

    tg_api_id   = int(os.getenv("TELEGRAM_API_ID", "0"))
    tg_api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()

    if not tg_api_id or not tg_api_hash:
        log.warning("social_signals: TELEGRAM_API_ID / TELEGRAM_API_HASH not set — monitor disabled")
        return

    log.info("social_signals: starting alpha channel monitor (%d channels)", len(ALPHA_CHANNEL_IDS))

    try:
        client = TelegramClient(StringSession(TELEGRAM_SESSION), tg_api_id, tg_api_hash)
        await client.connect()

        # Resolve channel entities
        channel_entities = []
        for ch_id in ALPHA_CHANNEL_IDS:
            try:
                entity = await client.get_entity(int(ch_id))
                channel_entities.append(entity)
                log.info("social_signals: monitoring channel %s", ch_id)
            except Exception as exc:
                log.warning("social_signals: could not resolve channel %s: %s", ch_id, exc)

        if not channel_entities:
            log.warning("social_signals: no valid channels resolved — monitor exiting")
            await client.disconnect()
            return

        @client.on(events.NewMessage(chats=channel_entities))
        async def _on_message(event):
            text = event.raw_text or ""
            channel_id = str(event.chat_id)
            now = time.time()

            # Extract Solana and EVM addresses from message
            sol_addrs = _SOL_ADDR_RE.findall(text)
            evm_addrs = _EVM_ADDR_RE.findall(text)
            found = [a.lower() for a in sol_addrs + evm_addrs]

            async with _alpha_lock:
                for addr in found:
                    # Trim old hits outside the window
                    _alpha_hits[addr] = [
                        (ch, ts) for ch, ts in _alpha_hits[addr]
                        if now - ts < ALPHA_WINDOW_SECONDS
                    ]
                    # Add this hit if channel not already recorded in window
                    existing_channels = {ch for ch, _ in _alpha_hits[addr]}
                    if channel_id not in existing_channels:
                        _alpha_hits[addr].append((channel_id, now))
                        channels_count = len(_alpha_hits[addr])
                        if channels_count >= ALPHA_MIN_CHANNELS:
                            log.info(
                                "social_signals: MULTI_CHANNEL_ALPHA %s in %d channels",
                                addr[:16], channels_count
                            )

        log.info("social_signals: alpha channel monitor running")
        await client.run_until_disconnected()

    except Exception as exc:
        log.error("social_signals: alpha channel monitor crashed: %s", exc)

# ── Scanner-facing API ────────────────────────────────────────────────────────

def get_alpha_channel_boost(token_addr: str) -> tuple[int, list[str]]:
    """
    Synchronous check — safe to call from scanner's process_pair().
    Returns (score_boost, flags).
    Boost is ALPHA_SIGNAL_BOOST if the token appeared in 2+ alpha channels
    within the last ALPHA_WINDOW_SECONDS seconds.
    """
    addr_lower = token_addr.lower()
    now = time.time()

    hits = [
        (ch, ts) for ch, ts in _alpha_hits.get(addr_lower, [])
        if now - ts < ALPHA_WINDOW_SECONDS
    ]
    unique_channels = len({ch for ch, _ in hits})

    if unique_channels >= ALPHA_MIN_CHANNELS:
        flags = [
            f"+{ALPHA_SIGNAL_BOOST} MULTI_CHANNEL_ALPHA ({unique_channels} channels)"
        ]
        return ALPHA_SIGNAL_BOOST, flags

    return 0, []


async def get_social_score_boost(
    token_addr: str,
    chain: str,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[int, list[str]]:
    """
    Combined social score boost for a token address.
    Calls both alpha channel check (sync, instant) and Arkham (async, network).
    Returns (total_boost, combined_flags).
    """
    total_boost = 0
    all_flags: list[str] = []

    # 1. Alpha channel check (instant, no network)
    alpha_boost, alpha_flags = get_alpha_channel_boost(token_addr)
    total_boost  += alpha_boost
    all_flags    += alpha_flags

    # 2. Arkham smart money check (network, opt-in)
    arkham_boost, arkham_flags = await get_arkham_wallet_activity(token_addr, chain, client)
    total_boost  += arkham_boost
    all_flags    += arkham_flags

    return total_boost, all_flags


# ── Telegram /alpha command ───────────────────────────────────────────────────

async def handle_alpha_command(client: httpx.AsyncClient, send_fn) -> None:
    """
    Handle /alpha Telegram command.
    Returns the last 10 contract addresses seen in alpha channels with timestamps.
    send_fn is the scanner's send_message function.
    """
    now = time.time()

    # Collect addresses with hits in the last hour
    recent: list[tuple[str, int, float]] = []  # (addr, channel_count, latest_ts)
    for addr, hits in list(_alpha_hits.items()):
        valid = [(ch, ts) for ch, ts in hits if now - ts < 3600]
        if valid:
            latest = max(ts for _, ts in valid)
            unique_chs = len({ch for ch, _ in valid})
            recent.append((addr, unique_chs, latest))

    # Sort by most recent
    recent.sort(key=lambda x: x[2], reverse=True)
    recent = recent[:10]

    if not recent:
        await send_fn(client, "No alpha channel hits in the last hour.")
        return

    lines = ["<b>Recent Alpha Channel Hits</b>\n"]
    for addr, ch_count, ts in recent:
        age_min = int((now - ts) / 60)
        multi = " MULTI" if ch_count >= ALPHA_MIN_CHANNELS else ""
        lines.append(f"{addr[:20]}...  |  {ch_count} ch{multi}  |  {age_min}m ago")

    await send_fn(client, "\n".join(lines))
