"""
evm_executor.py — Base chain DEX execution via Odos aggregator.

Replaces the broken Uniswap V3 direct-call implementation.
Uses Odos (https://api.odos.xyz) — free, no API key, aggregates across
Uniswap V3, Aerodrome, and all major Base DEXes for best execution.

Pattern mirrors solana_executor.py (Jupiter): quote → assemble → sign → send.

Env vars:
  EVM_WALLET_ADDRESS       — Public address of the hot wallet
  EVM_WALLET_PRIVATE_KEY   — Private key (from Vaultwarden, injected at runtime)
  EVM_BASE_RPC_URL         — Alchemy/Infura RPC for Base mainnet
  EVM_ARBITRUM_RPC_URL     — Alchemy/Infura RPC for Arbitrum mainnet
  EVM_MAX_SLIPPAGE_BPS     — Max slippage in basis points (default 100 = 1%)

Dependencies: pip install web3 eth-account requests
"""

import logging
import os
import time
from pathlib import Path
from typing import Literal, Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
WALLET_ADDRESS   = os.getenv("EVM_WALLET_ADDRESS", "").strip()
BASE_RPC         = os.getenv("EVM_BASE_RPC_URL", "").strip()
ARBITRUM_RPC     = os.getenv("EVM_ARBITRUM_RPC_URL", "").strip()
MAX_SLIPPAGE_BPS = int(os.getenv("EVM_MAX_SLIPPAGE_BPS", "100"))

# ── Odos aggregator ───────────────────────────────────────────────────────────
ODOS_QUOTE_URL    = "https://api.odos.xyz/sor/quote/v2"
ODOS_ASSEMBLE_URL = "https://api.odos.xyz/sor/assemble"

# Chain IDs
_CHAIN_IDS = {"base": 8453, "arbitrum": 42161}

# Well-known tokens
USDC_BASE     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_ARBITRUM = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
NATIVE_ETH    = "0x0000000000000000000000000000000000000000"  # Odos uses zero-address for native

Chain = Literal["base", "arbitrum"]
_REQUEST_TIMEOUT = 15
_APPROVAL_GAS = 60_000  # ERC-20 approve is ~46k gas, pad for safety


# ── RPC helpers ────────────────────────────────────────────────────────────────

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


def _get_nonce(chain: Chain) -> int:
    rpc = _check_rpc(chain)
    hex_nonce = _rpc_call(rpc, "eth_getTransactionCount", [WALLET_ADDRESS, "latest"])
    return int(hex_nonce, 16)


def _get_gas_price(chain: Chain) -> dict:
    """Return maxFeePerGas and maxPriorityFeePerGas for EIP-1559 tx."""
    rpc = _check_rpc(chain)
    # Get base fee from latest block
    block = _rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])
    base_fee = int(block["baseFeePerGas"], 16)
    # Priority fee: 0.1 gwei on Base (very low), 0.01 gwei minimum
    priority = max(100_000_000, base_fee // 10)  # 0.1 gwei or 10% of base
    max_fee = base_fee * 2 + priority  # 2x base + priority for safety
    return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}


# ── Balance checks ─────────────────────────────────────────────────────────────

def get_eth_balance(chain: Chain, address: str | None = None) -> float:
    """Return native ETH balance in ETH (float)."""
    rpc = _check_rpc(chain)
    addr = address or WALLET_ADDRESS
    if not addr:
        raise RuntimeError("EVM_WALLET_ADDRESS not set in .env.")
    hex_balance = _rpc_call(rpc, "eth_getBalance", [addr, "latest"])
    balance_eth = int(hex_balance, 16) / 1e18
    logger.info("ETH balance on %s for %s: %.6f ETH", chain, addr[:8] + "...", balance_eth)
    return balance_eth


def get_token_balance(chain: Chain, token_address: str, address: str | None = None) -> int:
    """Return raw ERC-20 token balance (smallest unit)."""
    rpc = _check_rpc(chain)
    addr = address or WALLET_ADDRESS
    if not addr:
        raise RuntimeError("EVM_WALLET_ADDRESS not set in .env.")
    padded_addr = addr[2:].lower().zfill(64)
    call_data = "0x70a08231" + padded_addr
    hex_balance = _rpc_call(rpc, "eth_call",
                            [{"to": token_address, "data": call_data}, "latest"])
    balance = int(hex_balance, 16) if hex_balance and hex_balance != "0x" else 0
    logger.info("Token %s balance on %s: %d (raw)", token_address[:10], chain, balance)
    return balance


def _get_allowance(chain: Chain, token: str, spender: str) -> int:
    """Check ERC-20 allowance for spender."""
    rpc = _check_rpc(chain)
    owner_pad = WALLET_ADDRESS[2:].lower().zfill(64)
    spender_pad = spender[2:].lower().zfill(64)
    call_data = "0xdd62ed3e" + owner_pad + spender_pad  # allowance(owner, spender)
    result = _rpc_call(rpc, "eth_call",
                       [{"to": token, "data": call_data}, "latest"])
    return int(result, 16) if result and result != "0x" else 0


# ── Token approval ─────────────────────────────────────────────────────────────

def _ensure_approval(chain: Chain, token: str, spender: str,
                     amount: int, private_key: str) -> Optional[str]:
    """
    Check allowance; if insufficient, send an approve(max) transaction.
    Returns the approval tx hash if one was sent, else None.
    """
    from eth_account import Account

    current = _get_allowance(chain, token, spender)
    if current >= amount:
        logger.info("Allowance sufficient (%d >= %d) for %s", current, amount, spender[:10])
        return None

    logger.info("Approving %s for spender %s on %s", token[:10], spender[:10], chain)
    max_uint = 2**256 - 1
    spender_pad = spender[2:].lower().zfill(64)
    amount_pad = hex(max_uint)[2:].zfill(64)
    call_data = "0x095ea7b3" + spender_pad + amount_pad

    rpc = _check_rpc(chain)
    nonce = _get_nonce(chain)
    gas_prices = _get_gas_price(chain)
    chain_id = _CHAIN_IDS[chain]

    tx = {
        "from": WALLET_ADDRESS,
        "to": token,
        "data": call_data,
        "nonce": nonce,
        "gas": _APPROVAL_GAS,
        "maxFeePerGas": gas_prices["maxFeePerGas"],
        "maxPriorityFeePerGas": gas_prices["maxPriorityFeePerGas"],
        "chainId": chain_id,
        "value": 0,
        "type": 2,  # EIP-1559
    }

    signed = Account.sign_transaction(tx, private_key)
    raw_hex = "0x" + signed.raw_transaction.hex()
    tx_hash = _rpc_call(rpc, "eth_sendRawTransaction", [raw_hex])
    logger.info("Approval tx sent: %s", tx_hash)

    # Wait for confirmation (up to 30s)
    for _ in range(15):
        time.sleep(2)
        try:
            receipt = _rpc_call(rpc, "eth_getTransactionReceipt", [tx_hash])
            if receipt and receipt.get("status") == "0x1":
                logger.info("Approval confirmed in block %s",
                            int(receipt["blockNumber"], 16))
                return tx_hash
            elif receipt and receipt.get("status") == "0x0":
                raise RuntimeError(f"Approval tx reverted: {tx_hash}")
        except RuntimeError as e:
            if "error" in str(e).lower():
                raise
            continue  # receipt not yet available

    raise RuntimeError(f"Approval tx not confirmed after 30s: {tx_hash}")


# ── Odos quote ─────────────────────────────────────────────────────────────────

def get_quote(chain: Chain, token_in: str, token_out: str,
              amount_in: int, **kwargs) -> dict:
    """
    Get a swap quote from Odos aggregator.
    Works with ANY token pair — not limited to a hardcoded list.

    Returns: {amount_in, amount_out, price_impact, path_id, gas_estimate, chain}
    """
    chain_id = _CHAIN_IDS.get(chain)
    if not chain_id:
        raise ValueError(f"Unsupported chain: {chain}")

    body = {
        "chainId": chain_id,
        "inputTokens": [{"tokenAddress": token_in, "amount": str(amount_in)}],
        "outputTokens": [{"tokenAddress": token_out, "proportion": 1}],
        "userAddr": WALLET_ADDRESS,
        "slippageLimitPercent": MAX_SLIPPAGE_BPS / 100,  # Odos expects percentage
        "referralCode": 0,
        "disableRFQs": True,
        "compact": True,
    }

    resp = requests.post(ODOS_QUOTE_URL, json=body, timeout=_REQUEST_TIMEOUT,
                         headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    data = resp.json()

    if "pathId" not in data:
        logger.warning("Odos quote failed: %s", data.get("message", data))
        return {"amount_in": amount_in, "amount_out": 0,
                "price_impact": None, "path_id": None, "chain": chain}

    # Extract output amount (Odos returns as outAmounts list)
    out_amounts = data.get("outAmounts", [])
    amount_out = int(out_amounts[0]) if out_amounts else 0

    price_impact = data.get("percentDiff", None)
    gas_estimate = data.get("gasEstimate", 0)
    path_id = data["pathId"]

    logger.info(
        "Odos quote on %s: %s → %s | in=%d out=%d impact=%.2f%% gas=%d",
        chain, token_in[:10], token_out[:10],
        amount_in, amount_out,
        float(price_impact) if price_impact else 0,
        gas_estimate,
    )

    return {
        "amount_in": amount_in,
        "amount_out": amount_out,
        "price_impact": price_impact,
        "path_id": path_id,
        "gas_estimate": gas_estimate,
        "chain": chain,
    }


# ── Odos tx assembly ──────────────────────────────────────────────────────────

def build_swap_tx(chain: Chain, token_in: str, token_out: str,
                  amount_in: int, amount_out_minimum: int,
                  path_id: Optional[str] = None, **kwargs) -> dict:
    """
    Assemble a swap transaction via Odos.
    If path_id is provided (from get_quote), uses that route.
    Otherwise, fetches a fresh quote first.

    Returns a complete EIP-1559 transaction dict ready for eth_account.sign_transaction().
    """
    if not path_id:
        # Get fresh quote
        quote = get_quote(chain, token_in, token_out, amount_in)
        path_id = quote.get("path_id")
        if not path_id:
            raise RuntimeError(
                f"No Odos route available for {token_in[:10]}→{token_out[:10]} on {chain}"
            )

    body = {
        "pathId": path_id,
        "userAddr": WALLET_ADDRESS,
        "simulate": False,
    }

    resp = requests.post(ODOS_ASSEMBLE_URL, json=body, timeout=_REQUEST_TIMEOUT,
                         headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    data = resp.json()

    if "transaction" not in data:
        raise RuntimeError(f"Odos assemble failed: {data.get('message', data)}")

    odos_tx = data["transaction"]
    chain_id = _CHAIN_IDS[chain]
    nonce = _get_nonce(chain)
    gas_prices = _get_gas_price(chain)

    # Odos returns: to, data, value, gas (estimated)
    gas_limit = int(odos_tx.get("gas", 300_000))
    gas_limit = int(gas_limit * 1.2)  # 20% buffer

    tx = {
        "from": WALLET_ADDRESS,
        "to": odos_tx["to"],
        "data": odos_tx["data"],
        "value": int(odos_tx.get("value", "0"), 16) if isinstance(odos_tx.get("value"), str) else int(odos_tx.get("value", 0)),
        "nonce": nonce,
        "gas": gas_limit,
        "maxFeePerGas": gas_prices["maxFeePerGas"],
        "maxPriorityFeePerGas": gas_prices["maxPriorityFeePerGas"],
        "chainId": chain_id,
        "type": 2,  # EIP-1559
    }

    # Store Odos router address for approval check
    tx["_odos_router"] = odos_tx["to"]

    logger.info(
        "Swap tx assembled: %s→%s gas=%d nonce=%d chain=%s router=%s",
        token_in[:10], token_out[:10], gas_limit, nonce, chain, odos_tx["to"][:10],
    )
    return tx


# ── Transaction broadcast ─────────────────────────────────────────────────────

def send_raw_transaction(chain: Chain, signed_tx_hex: str) -> str:
    """Broadcast a signed raw transaction to the chain RPC."""
    rpc = _check_rpc(chain)
    if not signed_tx_hex.startswith("0x"):
        signed_tx_hex = "0x" + signed_tx_hex
    result = _rpc_call(rpc, "eth_sendRawTransaction", [signed_tx_hex])
    tx_hash = result if isinstance(result, str) else str(result)
    logger.info("Transaction broadcast on %s: %s", chain, tx_hash)
    return tx_hash


def confirm_tx(chain: Chain, tx_hash: str,
               max_retries: int = 30, poll_seconds: float = 2.0) -> bool:
    """
    Poll for transaction receipt. Returns True if status=0x1 (success).
    """
    rpc = _check_rpc(chain)
    for attempt in range(max_retries):
        time.sleep(poll_seconds)
        try:
            receipt = _rpc_call(rpc, "eth_getTransactionReceipt", [tx_hash])
            if receipt:
                status = receipt.get("status", "0x0")
                block = int(receipt.get("blockNumber", "0x0"), 16)
                gas_used = int(receipt.get("gasUsed", "0x0"), 16)
                if status == "0x1":
                    logger.info("Tx confirmed: %s block=%d gas=%d", tx_hash, block, gas_used)
                    return True
                else:
                    logger.error("Tx reverted: %s block=%d", tx_hash, block)
                    return False
        except RuntimeError:
            continue
    logger.warning("Tx not confirmed after %ds: %s", max_retries * poll_seconds, tx_hash)
    return False


# ── High-level execute ─────────────────────────────────────────────────────────

def execute_swap(chain: Chain, token_in: str, token_out: str,
                 amount_in: int, private_key: str) -> dict:
    """
    Complete swap execution: quote → approve → assemble → sign → send → confirm.

    This is the preferred entry point — handles the full lifecycle.

    Args:
        chain:       "base" or "arbitrum"
        token_in:    Input token address (e.g., USDC)
        token_out:   Output token address (meme coin)
        amount_in:   Amount in token_in's smallest unit (e.g., 1_000_000 = 1 USDC)
        private_key: EVM wallet private key

    Returns:
        {"success": bool, "tx_hash": str, "amount_out": int, "error": str|None}
    """
    from eth_account import Account

    try:
        # 1. Check ETH for gas
        eth_bal = get_eth_balance(chain)
        if eth_bal < 0.0005:
            return {"success": False, "tx_hash": None, "amount_out": 0,
                    "error": f"Insufficient ETH for gas: {eth_bal:.6f}"}

        # 2. Check token_in balance
        token_bal = get_token_balance(chain, token_in)
        if token_bal < amount_in:
            return {"success": False, "tx_hash": None, "amount_out": 0,
                    "error": f"Insufficient {token_in[:10]} balance: {token_bal} < {amount_in}"}

        # 3. Get quote
        quote = get_quote(chain, token_in, token_out, amount_in)
        if not quote.get("path_id") or not quote.get("amount_out"):
            return {"success": False, "tx_hash": None, "amount_out": 0,
                    "error": f"No Odos route: {token_in[:10]}→{token_out[:10]}"}

        amount_out = quote["amount_out"]
        path_id = quote["path_id"]

        # 4. Assemble tx
        tx = build_swap_tx(chain, token_in, token_out, amount_in, 0, path_id=path_id)
        odos_router = tx.pop("_odos_router", tx["to"])

        # 5. Approve token_in for Odos router (if not native ETH)
        if token_in.lower() != NATIVE_ETH.lower():
            _ensure_approval(chain, token_in, odos_router, amount_in, private_key)
            # Re-fetch nonce after approval (approval used one nonce)
            tx["nonce"] = _get_nonce(chain)

        # 6. Sign
        signed = Account.sign_transaction(tx, private_key)
        raw_hex = "0x" + signed.raw_transaction.hex()

        # 7. Send
        tx_hash = send_raw_transaction(chain, raw_hex)

        # 8. Confirm
        confirmed = confirm_tx(chain, tx_hash)
        if not confirmed:
            return {"success": False, "tx_hash": tx_hash, "amount_out": amount_out,
                    "error": "Transaction not confirmed or reverted"}

        logger.info("Swap complete: %s→%s amount_out=%d tx=%s",
                    token_in[:10], token_out[:10], amount_out, tx_hash)
        return {"success": True, "tx_hash": tx_hash, "amount_out": amount_out, "error": None}

    except Exception as exc:
        logger.error("execute_swap failed: %s", exc, exc_info=True)
        return {"success": False, "tx_hash": None, "amount_out": 0, "error": str(exc)}


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"evm_executor self-test (Odos aggregator) | slippage={MAX_SLIPPAGE_BPS} bps")
    print(f"  EVM_WALLET_ADDRESS : {WALLET_ADDRESS or '(not set)'}")
    print(f"  BASE_RPC           : {'set' if BASE_RPC else '(not set)'}")
    print(f"  ARBITRUM_RPC       : {'set' if ARBITRUM_RPC else '(not set)'}")

    if BASE_RPC and WALLET_ADDRESS:
        try:
            bal = get_eth_balance("base")
            print(f"  Base ETH balance   : {bal:.6f} ETH {'(LOW)' if bal < 0.002 else '(OK)'}")
        except Exception as e:
            print(f"  get_eth_balance (base) failed: {e}")

        usdc_bal = get_token_balance("base", USDC_BASE)
        print(f"  Base USDC balance  : {usdc_bal / 1e6:.2f} USDC")

        # Test quote: 1 USDC → WETH (should always work)
        WETH_BASE = "0x4200000000000000000000000000000000000006"
        try:
            q = get_quote("base", USDC_BASE, WETH_BASE, 1_000_000)
            print(f"  Odos quote 1 USDC→WETH: out={q['amount_out']} "
                  f"impact={q.get('price_impact')}% path={q.get('path_id', 'none')[:16]}...")
        except Exception as e:
            print(f"  Odos quote failed: {e}")
    else:
        print("  EVM_BASE_RPC_URL or EVM_WALLET_ADDRESS not set — skipping tests")
