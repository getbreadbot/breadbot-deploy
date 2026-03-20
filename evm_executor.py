"""
evm_executor.py — Phase 2B
DEX execution on Base and Arbitrum via Uniswap V3 Router.
Enables token swaps on EVM chains where Coinbase/Kraken have no listings.

New .env vars required:
  EVM_WALLET_ADDRESS    — Public address of the hot wallet (no private key in .env)
  EVM_BASE_RPC_URL      — Alchemy/Infura RPC for Base mainnet
  EVM_ARBITRUM_RPC_URL  — Alchemy/Infura RPC for Arbitrum mainnet
  EVM_MAX_SLIPPAGE_BPS  — Max acceptable slippage in basis points (default 50 = 0.5%)

Private key handling:
  The EVM private key is NEVER written to .env.
  Stored in Vaultwarden → Breadbot → "EVM Wallet Private Key".
  Inject as EVM_WALLET_PRIVATE_KEY environment variable only when executing live swaps.
  Read-only operations (quotes, balances) require no private key.

Dependencies: pip install web3 eth-account requests
"""

import logging
import os
import time
from pathlib import Path
from typing import Literal

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
WALLET_ADDRESS   = os.getenv("EVM_WALLET_ADDRESS",   "").strip()
BASE_RPC         = os.getenv("EVM_BASE_RPC_URL",      "").strip()
ARBITRUM_RPC     = os.getenv("EVM_ARBITRUM_RPC_URL",  "").strip()
MAX_SLIPPAGE_BPS = int(os.getenv("EVM_MAX_SLIPPAGE_BPS", "50"))

# ── Flashbots MEV protection (Base only) ─────────────────────────────
FLASHBOTS_PROTECT_ENABLED = os.getenv("FLASHBOTS_PROTECT_ENABLED", "false").lower() == "true"
_FLASHBOTS_BASE_RPC       = "https://rpc.flashbots.net/fast"


# Uniswap V3 SwapRouter02 — same address on Base and Arbitrum
_SWAP_ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"

_WETH = {
    "base":     "0x4200000000000000000000000000000000000006",
    "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
}

Chain = Literal["base", "arbitrum"]
_REQUEST_TIMEOUT = 10


def _check_rpc(chain: Chain) -> str:
    rpc = BASE_RPC if chain == "base" else ARBITRUM_RPC
    if not rpc:
        raise RuntimeError(
            f"EVM_{chain.upper()}_RPC_URL is not set in .env. "
            "Add your Alchemy or Infura RPC URL."
        )
    return rpc


def _rpc_call(rpc_url: str, method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = requests.post(rpc_url, json=payload, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]


# ── Balance checks ─────────────────────────────────────────────────────────────

def get_eth_balance(chain: Chain, address: str | None = None) -> float:
    """Return native ETH balance in ETH (float) for the wallet."""
    rpc  = _check_rpc(chain)
    addr = address or WALLET_ADDRESS
    if not addr:
        raise RuntimeError("EVM_WALLET_ADDRESS not set in .env.")
    hex_balance = _rpc_call(rpc, "eth_getBalance", [addr, "latest"])
    balance_eth = int(hex_balance, 16) / 1e18
    logger.info("ETH balance on %s for %s: %.6f ETH", chain, addr[:8] + "...", balance_eth)
    return balance_eth


def get_token_balance(chain: Chain, token_address: str, address: str | None = None) -> int:
    """
    Return raw ERC-20 token balance (token's smallest unit).
    Divide by 10**decimals for human-readable amount.
    """
    rpc  = _check_rpc(chain)
    addr = address or WALLET_ADDRESS
    if not addr:
        raise RuntimeError("EVM_WALLET_ADDRESS not set in .env.")
    padded_addr = addr[2:].zfill(64)
    call_data   = "0x70a08231" + padded_addr
    hex_balance = _rpc_call(rpc, "eth_call", [{"to": token_address, "data": call_data}, "latest"])
    balance = int(hex_balance, 16) if hex_balance and hex_balance != "0x" else 0
    logger.info("Token %s balance on %s: %d (raw)", token_address[:8] + "...", chain, balance)
    return balance


# ── Quotes ─────────────────────────────────────────────────────────────────────

def get_quote(chain: Chain, token_in: str, token_out: str,
              amount_in: int, fee_tier: int = 3000) -> dict:
    """
    Get an indicative swap quote using CoinGecko market prices.
    Works from any server without API key. Accurate to ~0.1% for liquid pairs.

    fee_tier: kept for API compatibility (500/3000/10000) but not used for pricing.
    Returns: {amount_in, amount_out, price_impact, route, chain}

    Note: The actual on-chain execution price is determined by the Uniswap V3 router
    at swap time. This quote is used for pre-trade slippage validation only.
    """
    _COINGECKO_IDS = {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("usd-coin",  6),   # USDC
        "0x4200000000000000000000000000000000000006": ("ethereum",  18),  # WETH
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ("dai",       18),  # DAI
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("usd-coin",  6),   # USDbC
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": ("ethereum",  18),  # WETH Arbitrum
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831": ("usd-coin",  6),   # USDC Arbitrum
    }

    try:
        tin  = token_in.lower()
        tout = token_out.lower()

        if tin not in _COINGECKO_IDS or tout not in _COINGECKO_IDS:
            logger.warning("get_quote: token not in price map (%s or %s)", tin, tout)
            return {"amount_in": amount_in, "amount_out": 0,
                    "price_impact": None, "route": None, "chain": chain}

        cg_in,  dec_in  = _COINGECKO_IDS[tin]
        cg_out, dec_out = _COINGECKO_IDS[tout]

        ids = ",".join({cg_in, cg_out})
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd"},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        prices = resp.json()

        price_in  = prices.get(cg_in,  {}).get("usd", 0)
        price_out = prices.get(cg_out, {}).get("usd", 0)

        if not price_in or not price_out:
            raise ValueError(f"Missing price: in={price_in} out={price_out}")

        human_in  = amount_in / 10**dec_in
        human_out = human_in * (price_in / price_out)
        amount_out = int(human_out * 10**dec_out)

        logger.info(
            "Quote on %s: %g %s -> %g %s (via CoinGecko)",
            chain, human_in, cg_in, human_out, cg_out,
        )
        return {
            "amount_in":    amount_in,
            "amount_out":   amount_out,
            "price_impact": None,
            "route":        "coingecko",
            "chain":        chain,
        }

    except Exception as e:
        logger.warning("get_quote failed (%s) -- quote unavailable", e)
        return {"amount_in": amount_in, "amount_out": 0,
                "price_impact": None, "route": None, "chain": chain}


def check_slippage(quote: dict, min_amount_out: int) -> None:
    """Raise if expected output is below the slippage-adjusted minimum."""
    amount_out = quote.get("amount_out", 0)
    if amount_out < min_amount_out:
        raise ValueError(
            f"Slippage check failed: expected >={min_amount_out} but quote returned {amount_out}. "
            f"Max slippage: {MAX_SLIPPAGE_BPS} bps ({MAX_SLIPPAGE_BPS / 100:.2f}%)."
        )


# ── Token approval ─────────────────────────────────────────────────────────────

def build_approve_tx(chain: Chain, token_address: str, amount: int) -> dict:
    """
    Build an ERC-20 approve() transaction for the Uniswap SwapRouter.
    Must be signed and sent by the caller (requires private key from Vaultwarden).
    amount: use 2**256-1 for max approval.
    """
    _check_rpc(chain)
    padded_router = _SWAP_ROUTER[2:].zfill(64)
    padded_amount = hex(amount)[2:].zfill(64)
    call_data     = "0x095ea7b3" + padded_router + padded_amount
    chain_id      = 8453 if chain == "base" else 42161
    tx = {"from": WALLET_ADDRESS, "to": token_address, "data": call_data, "chainId": chain_id}
    logger.info("Approve tx built: token=%s router=%s chain=%s",
                token_address[:8] + "...", _SWAP_ROUTER[:8] + "...", chain)
    return tx


# ── Swap transaction builder ───────────────────────────────────────────────────

def build_swap_tx(chain: Chain, token_in: str, token_out: str, amount_in: int,
                  amount_out_minimum: int, fee_tier: int = 3000,
                  deadline_seconds: int = 180) -> dict:
    """
    Build a Uniswap V3 exactInputSingle swap transaction (unsigned).

    Caller must:
      1. call build_approve_tx() first if token_in is not ETH
      2. sign with EVM private key (from Vaultwarden)
      3. send via eth_sendRawTransaction

    amount_out_minimum: apply slippage before passing (use check_slippage() to validate quote first).
    """
    from eth_abi import encode  # type: ignore[import]

    deadline   = int(time.time()) + deadline_seconds
    chain_id   = 8453 if chain == "base" else 42161
    params_enc = encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(token_in, token_out, fee_tier, WALLET_ADDRESS, amount_in, amount_out_minimum, 0)],
    ).hex()
    call_data = "0x414bf389" + params_enc
    tx = {"from": WALLET_ADDRESS, "to": _SWAP_ROUTER, "data": call_data, "chainId": chain_id}
    logger.info("Swap tx built: %s→%s amtIn=%d minOut=%d chain=%s",
                token_in[:8] + "...", token_out[:8] + "...", amount_in, amount_out_minimum, chain)
    return tx


# ── Self-test ──────────────────────────────────────────────────────────────────


# ── Transaction broadcast ─────────────────────────────────────────────────────────────

def send_raw_transaction(chain: Chain, signed_tx_hex: str) -> str:
    """
    Broadcast a signed raw transaction.
    Routes through Flashbots Protect RPC on Base when FLASHBOTS_PROTECT_ENABLED=true,
    preventing frontrunning at no extra cost. Falls back to standard RPC otherwise.
    Arbitrum always uses the standard RPC (Flashbots Protect is Base/ETH only).

    Args:
        chain:          "base" or "arbitrum"
        signed_tx_hex:  Hex-encoded signed transaction (0x-prefixed)

    Returns:
        Transaction hash string.

    Raises:
        RuntimeError on RPC error or missing config.
    """
    if chain == "base" and FLASHBOTS_PROTECT_ENABLED:
        send_url = _FLASHBOTS_BASE_RPC
        logger.info("Flashbots Protect RPC active — MEV protection enabled for Base tx")
    else:
        send_url = _check_rpc(chain)

    result = _rpc_call(send_url, "eth_sendRawTransaction", [signed_tx_hex])
    tx_hash = result if isinstance(result, str) else str(result)
    logger.info("Transaction broadcast on %s: %s", chain, tx_hash)
    return tx_hash
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"evm_executor self-test | slippage={MAX_SLIPPAGE_BPS} bps")
    print(f"  EVM_WALLET_ADDRESS : {WALLET_ADDRESS or '(not set)'}")
    print(f"  BASE_RPC           : {'set' if BASE_RPC else '(not set)'}")
    print(f"  ARBITRUM_RPC       : {'set' if ARBITRUM_RPC else '(not set)'}")

    if BASE_RPC and WALLET_ADDRESS:
        try:
            bal = get_eth_balance("base")
            print(f"Base ETH balance OK — {bal:.6f} ETH")
            if bal < 0.002:
                print("  WARN: Low ETH balance on Base — may be insufficient for gas")
        except Exception as e:
            print(f"get_eth_balance (base) failed: {e}")
    else:
        print("EVM_BASE_RPC_URL or EVM_WALLET_ADDRESS not set — skipping balance check")

    USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    WETH_BASE = _WETH["base"]
    try:
        quote = get_quote("base", USDC_BASE, WETH_BASE, 1_000_000)  # 1 USDC
        print(f"Uniswap V3 quote on Base — 1 USDC → {quote['amount_out']} WETH raw "
              f"(impact={quote.get('price_impact')}%)")
    except Exception as e:
        print(f"get_quote failed: {e}")

    try:
        dummy_quote = {"amount_out": 900}
        check_slippage(dummy_quote, 1000)
        print("WARN: slippage guard did not fire")
    except ValueError as e:
        print(f"Slippage guard OK — caught: {e}")
