"""
scanner/dexscreener.py — Polls DEXScreener for new token pairs on Solana and Base.
Runs every 5 minutes via the scheduler in main.py.
"""

import httpx
import asyncio
from loguru import logger
import config

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/"
CHAINS = ["solana", "base"]

async def fetch_new_pairs(min_liquidity: float, min_volume: float) -> list[dict]:
    """
    Fetches recently created token pairs from DEXScreener.
    Returns a filtered list of candidates that pass liquidity/volume thresholds.
    """
    candidates = []

    async with httpx.AsyncClient(timeout=15) as client:
        for chain in CHAINS:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search?q=&chainId={chain}"
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                pairs = data.get("pairs", [])
                logger.debug(f"DEXScreener {chain}: {len(pairs)} pairs returned")

                for pair in pairs:
                    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
                    volume_24h = pair.get("volume", {}).get("h24", 0) or 0
                    age_hours = _pair_age_hours(pair)

                    # Basic filters
                    if liquidity < min_liquidity:
                        continue
                    if volume_24h < min_volume:
                        continue
                    if age_hours > 72:          # skip tokens older than 3 days
                        continue

                    candidates.append({
                        "symbol":    pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                        "chain":     chain,
                        "contract":  pair.get("baseToken", {}).get("address", ""),
                        "price_usd": float(pair.get("priceUsd", 0) or 0),
                        "liquidity": liquidity,
                        "volume_24h": volume_24h,
                        "market_cap": pair.get("marketCap", 0) or 0,
                        "age_hours": age_hours,
                        "pair_address": pair.get("pairAddress", ""),
                        "dex":       pair.get("dexId", ""),
                    })

            except httpx.HTTPError as e:
                logger.warning(f"DEXScreener {chain} error: {e}")
            except Exception as e:
                logger.error(f"DEXScreener unexpected error ({chain}): {e}")

    logger.info(f"DEXScreener scan complete — {len(candidates)} candidates passed filters")
    return candidates


def _pair_age_hours(pair: dict) -> float:
    """Estimate token age in hours from pairCreatedAt timestamp."""
    import time
    created = pair.get("pairCreatedAt")
    if not created:
        return 999.0
    try:
        created_ms = int(created)
        now_ms = int(time.time() * 1000)
        return (now_ms - created_ms) / (1000 * 3600)
    except Exception:
        return 999.0
