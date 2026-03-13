"""
yields/monitor.py — Polls stablecoin yield rates across DeFi platforms.
Runs every hour. Saves to yield_snapshots table. Alerts on significant changes.
"""

import httpx
import asyncio
from datetime import datetime, timezone
from loguru import logger
import config

# DeFi Llama is free, no API key needed, covers Aave, Compound, Coinbase, Kraken
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"

# The pool IDs we care about (from DeFi Llama pool IDs)
TRACKED_PLATFORMS = [
    {
        "platform": "Coinbase",
        "asset": "USDC",
        "notes": "USDC Rewards — automatic, no DeFi needed",
        "search": {"project": "coinbase", "symbol": "USDC"},
        "fallback_apy": 4.1,   # their published rate — DeFi Llama may not track this
    },
    {
        "platform": "Coinbase Morpho",
        "asset": "USDC",
        "notes": "Available in Coinbase app → Earn",
        "search": {"project": "morpho", "symbol": "USDC", "chain": "Base"},
        "fallback_apy": None,
    },
    {
        "platform": "Aave V3",
        "asset": "USDC",
        "notes": "Base network — requires Coinbase Wallet + bridge",
        "search": {"project": "aave-v3", "symbol": "USDC", "chain": "Base"},
        "fallback_apy": None,
    },
    {
        "platform": "Aave V3",
        "asset": "USDC",
        "notes": "Arbitrum network",
        "search": {"project": "aave-v3", "symbol": "USDC", "chain": "Arbitrum"},
        "fallback_apy": None,
    },
    {
        "platform": "Compound V3",
        "asset": "USDC",
        "notes": "Base network",
        "search": {"project": "compound-v3", "symbol": "USDC", "chain": "Base"},
        "fallback_apy": None,
    },
    {
        "platform": "Kraken",
        "asset": "USDC",
        "notes": "Kraken Earn — requires staking in Kraken account",
        "search": {"project": "kraken", "symbol": "USDC"},
        "fallback_apy": None,
    },
]


async def fetch_yields() -> list[dict]:
    """
    Pull current yield rates from DeFi Llama.
    Returns list of dicts ready to insert into yield_snapshots.
    """
    results = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(DEFILLAMA_POOLS_URL)
            resp.raise_for_status()
            pools = resp.json().get("data", [])
    except Exception as e:
        logger.error(f"DeFi Llama fetch failed: {e}")
        pools = []

    for tracked in TRACKED_PLATFORMS:
        match = _find_pool(pools, tracked["search"])
        if match:
            apy = match.get("apy") or match.get("apyBase") or 0
            tvl = match.get("tvlUsd") or 0
        elif tracked["fallback_apy"] is not None:
            apy = tracked["fallback_apy"]
            tvl = None
        else:
            logger.debug(f"No pool found for {tracked['platform']} {tracked['asset']}")
            continue

        results.append({
            "platform":    tracked["platform"],
            "asset":       tracked["asset"],
            "apy":         round(float(apy), 4),
            "tvl_usd":     float(tvl) if tvl else None,
            "notes":       tracked["notes"],
            "recorded_at": now,
        })
        logger.debug(f"  {tracked['platform']} {tracked['asset']}: {apy:.2f}%")

    logger.info(f"Yield poll complete — {len(results)} platforms updated")
    return results


def _find_pool(pools: list, criteria: dict) -> dict | None:
    """Find the best matching pool from DeFi Llama data."""
    candidates = []
    for pool in pools:
        match = True
        if "project" in criteria:
            if criteria["project"].lower() not in (pool.get("project") or "").lower():
                match = False
        if "symbol" in criteria:
            if criteria["symbol"].upper() not in (pool.get("symbol") or "").upper():
                match = False
        if "chain" in criteria:
            if criteria["chain"].lower() != (pool.get("chain") or "").lower():
                match = False
        if match:
            candidates.append(pool)

    if not candidates:
        return None

    # Pick the pool with highest TVL (most liquid / most likely the main pool)
    return max(candidates, key=lambda p: p.get("tvlUsd") or 0)


async def save_yields(db, yields: list[dict]):
    """Insert yield snapshots into the database."""
    for y in yields:
        await db.execute(
            """INSERT INTO yield_snapshots (platform, asset, apy, tvl_usd, notes, recorded_at)
               VALUES (:platform, :asset, :apy, :tvl_usd, :notes, :recorded_at)""",
            y
        )
    await db.commit()
    logger.debug(f"Saved {len(yields)} yield snapshots to DB")
