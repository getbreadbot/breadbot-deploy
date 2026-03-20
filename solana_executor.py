"""
solana_executor.py — Phase 2A
Jupiter V6 aggregator integration for Solana DEX execution.
No API key required. Quotes across every Solana DEX, executes at best price.

New .env vars required:
  SOLANA_WALLET_PUBKEY      — public key of the signing wallet
  SOLANA_PRIVATE_KEY        — base58-encoded private key (stored in Vaultwarden → Breadbot)
  SOLANA_RPC_URL            — Helius or QuickNode RPC endpoint
  SOLANA_MAX_SLIPPAGE_BPS   — max slippage in basis points (default 50 = 0.5%)
"""

import os
import base64
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
JUPITER_QUOTE_URL  = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL   = "https://lite-api.jup.ag/swap/v1/swap"

WALLET_PUBKEY      = os.getenv("SOLANA_WALLET_PUBKEY", "").strip()
PRIVATE_KEY_B58    = os.getenv("SOLANA_PRIVATE_KEY",   "").strip()
RPC_URL            = os.getenv("SOLANA_RPC_URL",        "https://api.mainnet-beta.solana.com").strip()
MAX_SLIPPAGE_BPS   = int(os.getenv("SOLANA_MAX_SLIPPAGE_BPS", "50"))

# USDC on Solana
USDC_MINT          = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ── Jito MEV protection ─────────────────────────────────────────────────────────────
JITO_ENABLED      = os.getenv("JITO_ENABLED",      "false").lower() == "true"
JITO_TIP_LAMPORTS = int(os.getenv("JITO_TIP_LAMPORTS", "500000"))
JITO_ENDPOINT     = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"



# ── Public API ────────────────────────────────────────────────────────────────

def get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int | None = None,
) -> dict:
    """
    Fetch the best swap route from Jupiter V6.

    Args:
        input_mint:      Token mint address to sell.
        output_mint:     Token mint address to buy.
        amount_lamports: Amount to sell in smallest unit (lamports / token decimals).
        slippage_bps:    Slippage tolerance in basis points. Defaults to SOLANA_MAX_SLIPPAGE_BPS.

    Returns:
        Jupiter quote dict on success.

    Raises:
        RuntimeError on HTTP error or empty route.
    """
    slippage = slippage_bps if slippage_bps is not None else MAX_SLIPPAGE_BPS
    params = {
        "inputMint":   input_mint,
        "outputMint":  output_mint,
        "amount":      amount_lamports,
        "slippageBps": slippage,
    }
    resp = requests.get(JUPITER_QUOTE_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data or "outAmount" not in data:
        raise RuntimeError(f"Jupiter returned no route for {input_mint} → {output_mint}")
    logger.info(
        "Quote: %s → %s | in=%s out=%s slippage=%sbps",
        input_mint[:8], output_mint[:8], amount_lamports, data["outAmount"], slippage,
    )
    return data


def build_swap_tx(quote: dict, user_public_key: str | None = None) -> str:
    """
    Request a serialized swap transaction from Jupiter.

    Args:
        quote:            The quote dict returned by get_quote().
        user_public_key:  Wallet public key. Defaults to SOLANA_WALLET_PUBKEY.

    Returns:
        Base64-encoded serialized transaction string.

    Raises:
        RuntimeError if Jupiter cannot build the transaction.
    """
    pubkey = user_public_key or WALLET_PUBKEY
    if not pubkey:
        raise RuntimeError("SOLANA_WALLET_PUBKEY is not configured in .env")

    payload = {
        "quoteResponse":            quote,
        "userPublicKey":            pubkey,
        "wrapAndUnwrapSol":         True,
        "dynamicComputeUnitLimit":  True,
        "prioritizationFeeLamports": "auto",
    }
    resp = requests.post(JUPITER_SWAP_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    tx_b64 = data.get("swapTransaction")
    if not tx_b64:
        raise RuntimeError(f"Jupiter did not return a swap transaction: {data}")
    logger.info("Swap transaction built (%d bytes serialized)", len(tx_b64))
    return tx_b64


def sign_and_send(tx_b64: str) -> str:
    """
    Sign and broadcast a serialized Solana transaction.

    Requires solana-py and base58 packages:
        pip install solana base58

    Args:
        tx_b64: Base64-encoded serialized transaction from build_swap_tx().

    Returns:
        Transaction signature string.

    Raises:
        RuntimeError if private key is missing or the RPC rejects the transaction.
    """
    if not PRIVATE_KEY_B58:
        raise RuntimeError(
            "SOLANA_PRIVATE_KEY is not set in .env. "
            "Add it from Vaultwarden → Breadbot → Solana Wallet Private Key."
        )

    try:
        from solders.keypair import Keypair          # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
        import base58                                # type: ignore
    except ImportError as e:
        raise ImportError(
            "Missing dependencies. Run: pip install solana solders base58"
        ) from e

    # Decode keypair
    secret_bytes = base58.b58decode(PRIVATE_KEY_B58)
    keypair = Keypair.from_bytes(secret_bytes)

    # Deserialize, sign, re-serialize
    raw_tx = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    tx.sign([keypair])
    signed_tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")

    # Send via RPC
    rpc_payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "sendTransaction",
        "params":  [
            signed_tx_b64,
            {
                "encoding":            "base64",
                "skipPreflight":       False,
                "preflightCommitment": "confirmed",
                "maxRetries":          3,
            },
        ],
    }
    # Route through Jito Block Engine (MEV protected) or standard RPC
    send_url = JITO_ENDPOINT if JITO_ENABLED else RPC_URL
    if JITO_ENABLED:
        logger.info("Jito MEV protection active — routing via Block Engine")
    rpc_resp = requests.post(send_url, json=rpc_payload, timeout=30)
    rpc_resp.raise_for_status()
    result = rpc_resp.json()

    if "error" in result:
        raise RuntimeError(f"RPC rejected transaction: {result[error]}")

    sig = result["result"]
    logger.info("Transaction sent: %s", sig)
    return sig


def confirm_tx(signature: str, max_retries: int = 15, poll_seconds: float = 2.0) -> bool:
    """
    Poll the RPC until the transaction is confirmed or max_retries is exhausted.

    Args:
        signature:    Transaction signature returned by sign_and_send().
        max_retries:  Number of polling attempts before giving up.
        poll_seconds: Seconds between each poll.

    Returns:
        True if confirmed, False if timed out or transaction failed.
    """
    import time

    for attempt in range(1, max_retries + 1):
        payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "getSignatureStatuses",
            "params":  [[signature], {"searchTransactionHistory": True}],
        }
        resp = requests.post(RPC_URL, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json().get("result", {}).get("value", [None])[0]

        if result is None:
            logger.debug("confirm_tx attempt %d/%d: not yet found", attempt, max_retries)
        elif result.get("err"):
            logger.error("Transaction failed on-chain: %s", result["err"])
            return False
        elif result.get("confirmationStatus") in ("confirmed", "finalized"):
            logger.info("Transaction confirmed: %s", signature)
            return True

        time.sleep(poll_seconds)

    logger.warning("confirm_tx timed out after %d attempts for %s", max_retries, signature)
    return False


def check_sol_balance() -> float:
    """
    Return the SOL balance of the configured wallet in SOL (not lamports).
    Used to gate transactions — bot should not attempt a swap if SOL < ~0.001.
    """
    if not WALLET_PUBKEY:
        raise RuntimeError("SOLANA_WALLET_PUBKEY is not configured in .env")

    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getBalance",
        "params":  [WALLET_PUBKEY],
    }
    resp = requests.post(RPC_URL, json=payload, timeout=10)
    resp.raise_for_status()
    lamports = resp.json().get("result", {}).get("value", 0)
    sol = lamports / 1_000_000_000
    logger.debug("SOL balance: %.6f SOL (%d lamports)", sol, lamports)
    return sol


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("solana_executor self-test")

    # SOL → USDC quote for 0.01 SOL (10_000_000 lamports)
    SOL_MINT = "So11111111111111111111111111111111111111112"
    try:
        q = get_quote(SOL_MINT, USDC_MINT, 10_000_000)
        print("Quote OK — out amount: " + str(q["outAmount"]) + " USDC decimals")
    except Exception as e:
        print(f"Quote failed: {e}")

    if WALLET_PUBKEY:
        try:
            bal = check_sol_balance()
            print(f"SOL balance: {bal:.6f}")
        except Exception as e:
            print(f"Balance check failed: {e}")
    else:
        print("SOLANA_WALLET_PUBKEY not set — skipping balance check")
