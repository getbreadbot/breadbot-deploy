"""
aerodrome_monitor.py — Phase 2B
Monitors Aerodrome Finance LP positions on Base chain via EVM contract reads.
Uses evm_executor._rpc_call and _check_rpc for all on-chain interaction.

No new .env vars required — uses EVM_BASE_RPC_URL from evm_executor.

Aerodrome Factory (Base): 0x420DD381b31aEf6683db6B902084cB0FFECe40Da
"""

import logging
import math

import requests
from evm_executor import _rpc_call, _check_rpc

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
_REQUEST_TIMEOUT = 10

# Function selectors (keccak256 first 4 bytes)
_SEL_GET_RESERVES   = "0x0902f1ac"  # getReserves()
_SEL_TOKEN0         = "0x0dfe1681"  # token0()
_SEL_TOKEN1         = "0xd21220a7"  # token1()
_SEL_TOTAL_SUPPLY   = "0x18160ddd"  # totalSupply()
_SEL_BALANCE_OF     = "0x70a08231"  # balanceOf(address)
_SEL_DECIMALS       = "0x313ce567"  # decimals()
_SEL_REWARD_RATE    = "0x7b0a47ee"  # rewardRate()
_SEL_REWARD_TOKEN   = "0xf7c618c1"  # rewardToken()


def _call(rpc: str, to: str, data: str) -> str:
    """Shorthand for eth_call returning raw hex."""
    return _rpc_call(rpc, "eth_call", [{"to": to, "data": data}, "latest"])


def _decode_uint(hex_str: str) -> int:
    """Decode a single uint256 from hex."""
    if not hex_str or hex_str == "0x":
        return 0
    return int(hex_str, 16)


def _decode_address(hex_str: str) -> str:
    """Decode an address from a 32-byte ABI-encoded word."""
    if not hex_str or hex_str == "0x":
        return "0x" + "0" * 40
    clean = hex_str.replace("0x", "")
    return "0x" + clean[-40:]


def _get_token_price_usd(token_address: str) -> float:
    """
    Get token price in USD via CoinGecko (free tier, no key needed).
    Falls back to 0.0 on failure.
    """
    try:
        url = "https://api.coingecko.com/api/v3/simple/token_price/base"
        resp = requests.get(
            url,
            params={"contract_addresses": token_address, "vs_currencies": "usd"},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get(token_address.lower(), {}).get("usd", 0.0)
    except Exception as e:
        logger.warning("Price fetch failed for %s: %s", token_address[:10], e)
        return 0.0


# ── Public API ────────────────────────────────────────────────────────────────

def get_pool_apr(pool_address: str) -> dict:
    """
    Estimate APR for an Aerodrome pool using on-chain reward rate and reserves.

    Returns: {pool, token0, token1, reserve0, reserve1, total_supply,
              reward_rate, tvl_usd, apr_pct}

    APR = (reward_rate * 86400 * 365 * reward_price) / tvl_usd * 100
    """
    rpc = _check_rpc("base")

    # Pool token addresses
    t0_hex = _call(rpc, pool_address, _SEL_TOKEN0)
    t1_hex = _call(rpc, pool_address, _SEL_TOKEN1)
    token0 = _decode_address(t0_hex)
    token1 = _decode_address(t1_hex)

    # Reserves
    reserves_hex = _call(rpc, pool_address, _SEL_GET_RESERVES)
    clean = reserves_hex.replace("0x", "").ljust(192, "0")
    reserve0 = int(clean[:64], 16)
    reserve1 = int(clean[64:128], 16)

    # Decimals
    dec0 = _decode_uint(_call(rpc, token0, _SEL_DECIMALS))
    dec1 = _decode_uint(_call(rpc, token1, _SEL_DECIMALS))
    dec0 = dec0 if dec0 > 0 else 18
    dec1 = dec1 if dec1 > 0 else 18

    # Total supply
    total_supply = _decode_uint(_call(rpc, pool_address, _SEL_TOTAL_SUPPLY))

    # Prices
    price0 = _get_token_price_usd(token0)
    price1 = _get_token_price_usd(token1)

    tvl_usd = (reserve0 / 10**dec0) * price0 + (reserve1 / 10**dec1) * price1

    # Gauge reward rate — Aerodrome pools have a gauge; try reading rewardRate
    # from the pool address directly (works for vAMM/sAMM gauges)
    try:
        reward_rate = _decode_uint(_call(rpc, pool_address, _SEL_REWARD_RATE))
        reward_token_hex = _call(rpc, pool_address, _SEL_REWARD_TOKEN)
        reward_token = _decode_address(reward_token_hex)
        reward_price = _get_token_price_usd(reward_token)
        reward_per_year = (reward_rate / 1e18) * 86400 * 365
        apr_pct = (reward_per_year * reward_price / tvl_usd * 100) if tvl_usd > 0 else 0.0
    except Exception:
        reward_rate = 0
        apr_pct = 0.0

    result = {
        "pool": pool_address,
        "token0": token0,
        "token1": token1,
        "reserve0": reserve0,
        "reserve1": reserve1,
        "total_supply": total_supply,
        "tvl_usd": round(tvl_usd, 2),
        "apr_pct": round(apr_pct, 2),
    }
    logger.info("Pool %s: TVL=$%.2f APR=%.2f%%", pool_address[:10], tvl_usd, apr_pct)
    return result


def get_position_value(position_id: str) -> dict:
    """
    Get the USD value of an LP position by its NFT token ID or LP token balance.

    For Aerodrome, position_id is the pool address — we read the wallet's
    LP token balance and compute its share of TVL.

    Returns: {pool, lp_balance, total_supply, share_pct, value_usd}
    """
    from evm_executor import WALLET_ADDRESS

    rpc = _check_rpc("base")
    pool_address = position_id

    if not WALLET_ADDRESS:
        raise RuntimeError("EVM_WALLET_ADDRESS not set in .env.")

    # LP balance of wallet
    padded_addr = WALLET_ADDRESS[2:].zfill(64)
    balance_hex = _call(rpc, pool_address, _SEL_BALANCE_OF + padded_addr)
    lp_balance = _decode_uint(balance_hex)

    # Total supply
    total_supply = _decode_uint(_call(rpc, pool_address, _SEL_TOTAL_SUPPLY))

    if total_supply == 0:
        return {"pool": pool_address, "lp_balance": 0, "total_supply": 0,
                "share_pct": 0.0, "value_usd": 0.0}

    share_pct = (lp_balance / total_supply) * 100

    # Get pool TVL for value calc
    pool_info = get_pool_apr(pool_address)
    value_usd = pool_info["tvl_usd"] * (lp_balance / total_supply)

    result = {
        "pool": pool_address,
        "lp_balance": lp_balance,
        "total_supply": total_supply,
        "share_pct": round(share_pct, 6),
        "value_usd": round(value_usd, 2),
    }
    logger.info("Position %s: %.6f%% share = $%.2f", pool_address[:10], share_pct, value_usd)
    return result


def check_il(pool_address: str, entry_price_ratio: float) -> dict:
    """
    Check impermanent loss for a pool given the entry price ratio (token0/token1).

    Uses the standard IL formula:
        IL = 2*sqrt(r) / (1+r) - 1
    where r = current_ratio / entry_ratio

    Returns: {pool, entry_ratio, current_ratio, price_change_pct, il_pct}
    """
    rpc = _check_rpc("base")

    # Current reserves
    reserves_hex = _call(rpc, pool_address, _SEL_GET_RESERVES)
    clean = reserves_hex.replace("0x", "").ljust(192, "0")
    reserve0 = int(clean[:64], 16)
    reserve1 = int(clean[64:128], 16)

    if reserve1 == 0 or entry_price_ratio <= 0:
        return {"pool": pool_address, "entry_ratio": entry_price_ratio,
                "current_ratio": 0, "price_change_pct": 0, "il_pct": 0}

    # Adjust for decimals
    t0_hex = _call(rpc, pool_address, _SEL_TOKEN0)
    t1_hex = _call(rpc, pool_address, _SEL_TOKEN1)
    token0 = _decode_address(t0_hex)
    token1 = _decode_address(t1_hex)
    dec0 = _decode_uint(_call(rpc, token0, _SEL_DECIMALS)) or 18
    dec1 = _decode_uint(_call(rpc, token1, _SEL_DECIMALS)) or 18

    current_ratio = (reserve0 / 10**dec0) / (reserve1 / 10**dec1)
    r = current_ratio / entry_price_ratio
    il = 2 * math.sqrt(r) / (1 + r) - 1
    price_change_pct = (r - 1) * 100

    result = {
        "pool": pool_address,
        "entry_ratio": entry_price_ratio,
        "current_ratio": round(current_ratio, 8),
        "price_change_pct": round(price_change_pct, 2),
        "il_pct": round(il * 100, 4),
    }
    logger.info("IL check %s: ratio %.4f->%.4f IL=%.4f%%",
                pool_address[:10], entry_price_ratio, current_ratio, il * 100)
    return result


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("aerodrome_monitor self-test")
    # USDC/WETH sAMM pool on Aerodrome (example — replace with actual pool)
    TEST_POOL = "0xcDAC0d6c6C59727a65F871236188350531885C43"
    try:
        info = get_pool_apr(TEST_POOL)
        print(f"  Pool APR: {info}")
    except Exception as e:
        print(f"  get_pool_apr failed: {e}")
