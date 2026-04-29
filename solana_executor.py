"""
solana_executor.py — Jupiter V6 aggregator for Solana DEX execution
No API key required. Quotes across every Solana DEX, executes at best price.
Jito bundle submission for MEV protection.

.env vars required:
  SOLANA_WALLET_PUBKEY      — public key of the signing wallet
  SOLANA_PRIVATE_KEY        — base58-encoded private key (stored in Vaultwarden → Breadbot)
  SOLANA_RPC_URL            — Helius or QuickNode RPC endpoint
  SOLANA_MAX_SLIPPAGE_BPS   — max slippage in basis points (default 50 = 0.5%)

──────────────────────────────────────────────────────────────────────────
S55 P3 — confirm_tx error-code contract:
──────────────────────────────────────────────────────────────────────────
confirm_tx() returns (confirmed: bool, err_code: str | None):
  (True,  None)        — tx confirmed/finalized successfully
  (False, "SLIPPAGE")  — Jupiter program error Custom 6001 (slippage exceeded)
                         position_manager uses this to retry immediately
                         with escalated slippage (skip the 120s cooldown)
  (False, "FAILED")    — other on-chain program error
  (False, "TIMEOUT")   — no confirmation within max_retries polls

The SLIPPAGE classification is load-bearing for position_manager's retry
escalation. Do NOT collapse the return shape back to a plain bool without
first removing the escalation path in position_manager._evaluate_position.

Jupiter error code reference:
  6001 = SlippageToleranceExceeded (documented at
  https://github.com/jup-ag/jupiter-swap-api-client)
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
        # S75 P1: cap route complexity so transactions stay under Solana's 1232-byte
        # raw limit (~1644 bytes base64). Without this, Jupiter occasionally returns
        # multi-hop routes that build to 1644-1704 bytes serialized, which both Jito
        # and standard RPC reject as "decoded too large", causing SL bleeds while
        # the bot retries the same oversized payload through cooldown loops.
        "maxAccounts": 32,
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
        from solders.message import to_bytes_versioned  # type: ignore
        import base58                                # type: ignore
    except ImportError as e:
        raise ImportError(
            "Missing dependencies. Run: pip install solana solders base58"
        ) from e

    # Decode keypair
    secret_bytes = base58.b58decode(PRIVATE_KEY_B58)
    keypair = Keypair.from_bytes(secret_bytes)

    # Deserialize, sign (solders 0.26+), re-serialize
    raw_tx = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    msg = tx.message
    sig = keypair.sign_message(to_bytes_versioned(msg))
    signed_tx = VersionedTransaction.populate(msg, [sig])
    signed_tx_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")

    # Send via RPC
    rpc_payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "sendTransaction",
        "params":  [
            signed_tx_b64,
            {
                "encoding":            "base64",
                "skipPreflight":       JITO_ENABLED,  # Jito requires skipPreflight=True
                "preflightCommitment": "confirmed",
                "maxRetries":          3,
            },
        ],
    }
    # Route through Jito Block Engine (MEV protected) or standard RPC
    # If Jito returns 400 (e.g. "transaction #0 could not be decoded" — happens
    # intermittently on larger multi-hop txs or during Jito-side glitches), fall
    # back to the standard RPC so the trade still lands. We lose MEV protection
    # on fallback but avoid missing the trade entirely.
    result = None
    used_jito = False
    if JITO_ENABLED:
        used_jito = True
        logger.info("Jito MEV protection active — routing via Block Engine")
        rpc_resp = requests.post(JITO_ENDPOINT, json=rpc_payload, timeout=30)
        if rpc_resp.status_code == 200:
            result = rpc_resp.json()
            if "error" in result:
                logger.warning("Jito RPC error, falling back to standard RPC: %s", result.get("error"))
                result = None
                used_jito = False
        else:
            logger.warning(
                "Jito HTTP %d (%s) — falling back to standard RPC",
                rpc_resp.status_code, rpc_resp.text[:200]
            )
            used_jito = False
    if result is None:
        rpc_resp = requests.post(RPC_URL, json=rpc_payload, timeout=30)
        if rpc_resp.status_code != 200:
            logger.error("RPC HTTP %d: %s", rpc_resp.status_code, rpc_resp.text[:300])
            rpc_resp.raise_for_status()
        result = rpc_resp.json()
    logger.info("Submitted via %s", "Jito" if used_jito else "standard RPC")

    if "error" in result:
        raise RuntimeError(f"RPC rejected transaction: {result["error"]}")

    sig = result["result"]
    logger.info("Transaction sent: %s", sig)
    return sig


def confirm_tx(signature: str, max_retries: int = 15, poll_seconds: float = 2.0) -> tuple[bool, str | None]:
    """
    Poll the RPC until the transaction is confirmed or max_retries is exhausted.

    Args:
        signature:    Transaction signature returned by sign_and_send().
        max_retries:  Number of polling attempts before giving up.
        poll_seconds: Seconds between each poll.

    Returns:
        (confirmed, err_code) tuple.
          confirmed=True, err_code=None              -> success
          confirmed=False, err_code="SLIPPAGE"       -> Jupiter slippage (Custom 6001)
          confirmed=False, err_code="FAILED"         -> other on-chain program error
          confirmed=False, err_code="TIMEOUT"        -> no confirmation within retries

    S55 P3: err_code enables position_manager to retry slippage errors
    immediately with escalated tolerance instead of serving the 120s cooldown.
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
            err = result["err"]
            # Detect Jupiter slippage error: {"InstructionError":[N,{"Custom":6001}]}
            err_code = "FAILED"
            try:
                if isinstance(err, dict) and "InstructionError" in err:
                    inner = err["InstructionError"][1]
                    if isinstance(inner, dict) and inner.get("Custom") == 6001:
                        err_code = "SLIPPAGE"
            except (IndexError, KeyError, TypeError):
                pass
            logger.error("Transaction failed on-chain (%s): %s", err_code, err)
            return False, err_code
        elif result.get("confirmationStatus") in ("confirmed", "finalized"):
            logger.info("Transaction confirmed: %s", signature)
            return True, None

        time.sleep(poll_seconds)

    logger.warning("confirm_tx timed out after %d attempts for %s", max_retries, signature)
    return False, "TIMEOUT"


def close_empty_atas(mint_filter: str | None = None) -> int:
    """
    Close empty SPL token ATAs owned by the bot wallet, reclaiming ~0.00204 SOL
    of rent per ATA back to the wallet's native SOL balance.

    If mint_filter is provided, only close the ATA for that specific mint
    (used after a successful sell to reclaim that position's rent). If None,
    closes ALL empty ATAs owned by the wallet (sweep mode).

    USDC is always skipped even if its balance shows zero — closing the main
    stablecoin ATA would require re-creating it on the next trade.

    Returns the number of ATAs closed. Best-effort — errors are logged and
    swallowed so callers can treat this as fire-and-forget.

    S58 P0: added to prevent silent SOL drain from accumulated rug/loss ATAs.
    S60 P1: added sleep + Confirmed commitment to avoid Helius stale cache.
    S61 P1: now scans BOTH legacy SPL Token and Token-2022 programs. Pump.fun
            mints use Token-2022, so the legacy-only scan missed every
            post-sell pump.fun ATA (~13 leaked between S58 and S61).
            Each closeable entry is tagged with its owning program so the
            close_account instruction targets the correct one.
    """
    try:
        import base58
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction
        from solders.message import MessageV0
        from solana.rpc.api import Client
        from solana.rpc.commitment import Confirmed
        from solana.rpc.types import TokenAccountOpts, TxOpts
        from spl.token.constants import TOKEN_PROGRAM_ID
        from spl.token.instructions import close_account, CloseAccountParams
    except ImportError as exc:
        logger.debug("close_empty_atas: dependencies missing (%s) — skipping", exc)
        return 0

    if not WALLET_PUBKEY or not PRIVATE_KEY_B58:
        return 0

    # Token-2022 program id. Not exported as a constant by spl-token-py, so
    # we pin the canonical mainnet address here.
    TOKEN_2022_PROGRAM_ID = Pubkey.from_string(
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    )

    try:
        kp = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_B58))
        wallet = kp.pubkey()
        client = Client(RPC_URL)

        # S60 P1: Helius RPC default commitment can return a stale snapshot
        # immediately after confirm_tx. Sleep briefly and use Confirmed
        # commitment so we see the post-sell zero balance, not the pre-sell
        # cached balance. Without this, empty ATAs silently leak.
        import time
        time.sleep(3.0)

        closeable: list[tuple] = []  # (ata_pubkey, owning_program_id)
        for prog_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
            resp = client.get_token_accounts_by_owner_json_parsed(
                wallet,
                TokenAccountOpts(program_id=prog_id),
                commitment=Confirmed,
            )
            for a in (resp.value or []):
                info = a.account.data.parsed["info"]
                mint = info["mint"]
                bal = float(info["tokenAmount"]["uiAmountString"] or 0)
                if bal != 0:
                    continue
                if mint == USDC_MINT:
                    continue
                if mint_filter and mint != mint_filter:
                    continue
                closeable.append((a.pubkey, prog_id))

        if not closeable:
            return 0

        ixs = [
            close_account(CloseAccountParams(
                program_id=prog_id,
                account=ata, dest=wallet, owner=wallet,
            ))
            for ata, prog_id in closeable
        ]
        bh = client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(
            payer=wallet, instructions=ixs,
            address_lookup_table_accounts=[], recent_blockhash=bh,
        )
        tx = VersionedTransaction(msg, [kp])
        sig = client.send_raw_transaction(
            bytes(tx),
            opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
        )
        logger.info("close_empty_atas: closed %d ATAs sig=%s", len(closeable), sig.value)
        return len(closeable)
    except Exception as exc:
        logger.warning("close_empty_atas: best-effort close failed: %s", exc)
        return 0



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
