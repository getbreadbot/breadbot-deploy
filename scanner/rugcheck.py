"""
scanner/rugcheck.py — Security scoring via GoPlus and RugCheck.
Scores tokens 0-100. Score < 50 is blocked. Score 50-79 gets reduced position size.
"""

import httpx
import asyncio
from loguru import logger
import config

GOPLUS_URL   = "https://api.gopluslabs.io/api/v1/token_security/"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"

CHAIN_MAP = {
    "solana": "solana",
    "base":   "8453",       # GoPlus uses chain ID for EVM
}

# Point deductions per flag
DEDUCTIONS = {
    "is_honeypot":              40,
    "has_blacklist":            20,
    "has_whitelist":            10,
    "is_mintable":              15,
    "owner_not_renounced":      10,
    "buy_tax_high":             10,   # > 5%
    "sell_tax_high":            15,   # > 5%
    "top10_holders_high":       10,   # > 35%
    "lp_not_locked":            10,
    "transfer_pausable":        20,
    "rugcheck_failed":          15,
}


async def score_token(contract: str, chain: str) -> dict:
    """
    Run GoPlus + RugCheck on a contract address.
    Returns: {"score": int, "flags": [str], "raw": dict}
    """
    score = 100
    flags = []
    raw   = {}

    # ── GoPlus check ────────────────────────────────────────
    chain_id = CHAIN_MAP.get(chain)
    if chain_id:
        gp = await _goplus_check(contract, chain_id)
        raw["goplus"] = gp
        if gp:
            # Honeypot
            if str(gp.get("is_honeypot", "0")) == "1":
                flags.append("is_honeypot")
                score -= DEDUCTIONS["is_honeypot"]

            # Blacklist/whitelist functions
            if str(gp.get("is_blacklisted", "0")) == "1":
                flags.append("has_blacklist")
                score -= DEDUCTIONS["has_blacklist"]
            if str(gp.get("is_whitelisted", "0")) == "1":
                flags.append("has_whitelist")
                score -= DEDUCTIONS["has_whitelist"]

            # Mintable
            if str(gp.get("is_mintable", "0")) == "1":
                flags.append("is_mintable")
                score -= DEDUCTIONS["is_mintable"]

            # Owner not renounced
            if str(gp.get("owner_address", "")) not in ("", "0x0000000000000000000000000000000000000000"):
                flags.append("owner_not_renounced")
                score -= DEDUCTIONS["owner_not_renounced"]

            # Taxes
            try:
                if float(gp.get("buy_tax", 0) or 0) > 5:
                    flags.append("buy_tax_high")
                    score -= DEDUCTIONS["buy_tax_high"]
                if float(gp.get("sell_tax", 0) or 0) > 5:
                    flags.append("sell_tax_high")
                    score -= DEDUCTIONS["sell_tax_high"]
            except (ValueError, TypeError):
                pass

            # Top 10 holders
            try:
                holders = gp.get("holders", [])
                top10_pct = sum(float(h.get("percent", 0) or 0) for h in holders[:10])
                if top10_pct > config.MAX_TOP10_HOLDER_PCT:
                    flags.append("top10_holders_high")
                    score -= DEDUCTIONS["top10_holders_high"]
            except Exception:
                pass

            # LP locked
            if str(gp.get("lp_locked", "0")) != "1":
                flags.append("lp_not_locked")
                score -= DEDUCTIONS["lp_not_locked"]

            # Transfer pausable
            if str(gp.get("transfer_pausable", "0")) == "1":
                flags.append("transfer_pausable")
                score -= DEDUCTIONS["transfer_pausable"]

    # ── RugCheck (Solana only) ───────────────────────────────
    if chain == "solana":
        rc_ok = await _rugcheck(contract)
        raw["rugcheck"] = rc_ok
        if rc_ok is False:
            flags.append("rugcheck_failed")
            score -= DEDUCTIONS["rugcheck_failed"]

    score = max(0, score)
    logger.debug(f"Score {contract[:8]}…: {score} | flags: {flags}")
    return {"score": score, "flags": flags, "raw": raw}


async def _goplus_check(contract: str, chain_id: str) -> dict | None:
    headers = {}
    if config.GOPLUS_API_KEY:
        headers["Authorization"] = config.GOPLUS_API_KEY
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GOPLUS_URL}{chain_id}",
                params={"contract_addresses": contract},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", {})
            return result.get(contract.lower()) or result.get(contract)
    except Exception as e:
        logger.warning(f"GoPlus check failed for {contract[:8]}: {e}")
        return None


async def _rugcheck(contract: str) -> bool | None:
    """Returns True if passed, False if flagged, None if unavailable."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(RUGCHECK_URL.format(contract))
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            risks = data.get("risks", [])
            critical = [r for r in risks if r.get("level") == "critical"]
            return len(critical) == 0
    except Exception as e:
        logger.warning(f"RugCheck failed for {contract[:8]}: {e}")
        return None
