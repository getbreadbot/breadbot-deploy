#!/usr/bin/env python3
"""
exchange_executor.py — Routes approved auto-execute orders to the correct connector.

Called from scanner.process_pair() when AutoExecutor.evaluate() returns executed=True.
This module does NOT make trade decisions — AutoExecutor does that.
This module only routes and logs.

Execution routing:
  chain=solana  → solana_executor.sign_and_send() (Jupiter V6 + Jito MEV)
  chain=base    → evm_executor.execute_swap() (Odos DEX aggregator)
  fallback      → coinbase_connector or kraken_connector market order

All methods return bool. Any exception is caught, logged, and returns False.
No exception propagates to the scanner loop.
"""

import logging
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger("exchange_executor")

def _db_get_config(key: str) -> str:
    """Read a value from bot_config table (DB-first, same as AutoExecutor)."""
    db_path = Path(__file__).parent / "data" / "cryptobot.db"
    if not db_path.exists():
        return ""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0].strip() if row and row[0] else ""
    except Exception:
        return ""

# Lazy imports — connectors may not be fully configured on every deploy
def _try_import(module_name: str):
    try:
        import importlib
        return importlib.import_module(module_name)
    except Exception as exc:
        log.warning("Could not import %s: %s", module_name, exc)
        return None


def execute_trade(
    chain: str,
    token_addr: str,
    symbol: str,
    position_usd: float,
    price_usd: float,
    force: bool = False,
) -> bool:
    """
    Route and execute an auto-approved trade.

    Args:
        chain:        "solana" or "base"
        token_addr:   contract address of the token to buy
        symbol:       human-readable symbol (for logging)
        position_usd: dollar amount approved by AutoExecutor
        price_usd:    current price per token
        force:        if True, bypasses the execution_mode='auto' gate.
                      Used by manual buy paths (Research page) where the
                      user has explicitly initiated the trade. The pause
                      flag and daily loss limit are still respected via
                      the AutoExecutor.evaluate() called upstream.

    Returns:
        True if trade was submitted to the exchange successfully.
        False if execution was skipped, failed, or the connector is unconfigured.
    """
    if not force:
        # DB-first config read (matches AutoExecutor pattern)
        mode = _db_get_config("execution_mode") or os.getenv("EXECUTION_MODE", "manual").strip().lower()
        mode = mode.lower()
        if mode != "auto":
            log.debug("execute_trade called but EXECUTION_MODE=%s — skip", mode)
            return False

    if position_usd <= 0:
        log.warning("execute_trade: position_usd=%s — skip", position_usd)
        return False

    log.info(
        "Routing execution: chain=%s symbol=%s addr=%s position=$%.2f price=$%.8f",
        chain, symbol, token_addr[:12], position_usd, price_usd,
    )

    try:
        if chain == "solana":
            return _execute_solana(token_addr, symbol, position_usd, price_usd)
        elif chain == "base":
            return _execute_base(token_addr, symbol, position_usd, price_usd)
        elif chain == "cex":
            return _execute_cex(symbol, position_usd)
        else:
            log.warning("execute_trade: unknown chain=%s — skip", chain)
            return False
    except Exception as exc:
        log.error("execute_trade unhandled exception: %s", exc, exc_info=True)
        return False


def _execute_solana(token_addr: str, symbol: str, position_usd: float, price_usd: float) -> bool:
    """Execute via Jupiter V6 on Solana. MEV-protected via Jito when JITO_ENABLED=true."""
    sol_exec = _try_import("solana_executor")
    if sol_exec is None:
        log.error("solana_executor unavailable — cannot execute Solana trade for %s", symbol)
        return False

    wallet = os.getenv("SOLANA_WALLET_PUBKEY", "").strip()
    if not wallet:
        log.warning("SOLANA_WALLET_PUBKEY not configured — skipping Solana execution for %s", symbol)
        return False

    try:
        # S58 P0: Pre-flight SOL balance check.
        # Jupiter swaps routinely include a CreateIdempotent ATA instruction
        # for the output token mint. ATA creation requires ~0.00204 SOL of
        # rent-exempt deposit, separate from the ~0.000105 SOL tx fee.
        # Plus Jito tip (~0.0005 SOL when enabled). Threshold must cover all
        # three with a safety margin, otherwise we burn fees on txs that fail
        # at the ATA-create step with error Custom 0x1 (insufficient lamports).
        # Ref: jfiaFUEu... (ROCCO), 5mbMdyxs... (pipi) — both had wallet at
        # ~0.0017 SOL, needed ~0.002 for ATA rent.
        sol_balance = sol_exec.check_sol_balance()
        SOL_MIN_FOR_ENTRY = 0.005  # ~0.002 ATA + ~0.0005 Jito + ~0.0001 fee + buffer
        if sol_balance < SOL_MIN_FOR_ENTRY:
            log.warning(
                "INSUFFICIENT_SOL: balance=%.6f SOL < threshold=%.6f SOL — "
                "skipping %s (top up wallet %s to resume Solana entries)",
                sol_balance, SOL_MIN_FOR_ENTRY, symbol, wallet,
            )
            return False

        # Convert USD to lamports: 1 USDC = 1_000_000 lamports (6 decimals)
        usdc_amount_lamports = int(position_usd * 1_000_000)

        # S57 P0 fix: confirm_tx() returns a (confirmed, err_code) TUPLE,
        # not a bool. The old `if confirmed:` test was always truthy on
        # (False, "SLIPPAGE"), producing phantom position records for
        # trades that failed on-chain.
        #
        # S57 P1: escalate slippage on Jupiter err 6001 (SLIPPAGE) for
        # entry-side. Mirrors the sell-side schedule in position_manager
        # (S55 P3). Non-SLIPPAGE errors fail fast — retrying a FAILED
        # or TIMEOUT tx burns gas without changing the outcome.
        base_bps = int(os.getenv("SOLANA_MAX_SLIPPAGE_BPS", "50"))
        tiers = sorted({base_bps, 500, 1500, 3000})
        tiers = [t for t in tiers if t >= base_bps and t <= 3000]
        if not tiers:
            # Operator set SOLANA_MAX_SLIPPAGE_BPS above 3000 — honor that
            # rather than refusing to trade. Single attempt, no escalation.
            tiers = [base_bps]

        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

        for idx, slippage_bps in enumerate(tiers):
            quote = sol_exec.get_quote(
                USDC_MINT, token_addr, usdc_amount_lamports,
                slippage_bps=slippage_bps,
            )
            tx_b64 = sol_exec.build_swap_tx(quote)
            sig = sol_exec.sign_and_send(tx_b64)
            confirmed, err_code = sol_exec.confirm_tx(sig)

            if confirmed:
                log.info(
                    "Solana execution SUCCESS: %s $%.2f slippage=%dbps sig=%s",
                    symbol, position_usd, slippage_bps, sig,
                )
                return True

            # Only retry on SLIPPAGE; everything else fails fast.
            if err_code != "SLIPPAGE":
                log.warning(
                    "Solana execution failed (%s): %s $%.2f sig=%s — no retry",
                    err_code, symbol, position_usd, sig,
                )
                return False

            if idx + 1 < len(tiers):
                log.warning(
                    "Solana entry %s slippage=%dbps failed — escalating to %dbps",
                    symbol, slippage_bps, tiers[idx + 1],
                )
            else:
                log.error(
                    "Solana entry %s exhausted slippage tiers (last=%dbps sig=%s) — giving up",
                    symbol, slippage_bps, sig,
                )
                return False

        return False

    except Exception as exc:
        log.error("Solana execution failed for %s: %s", symbol, exc)
        return False


def _execute_base(token_addr: str, symbol: str, position_usd: float, price_usd: float) -> bool:
    """Execute via Odos DEX aggregator on Base. Handles quote, approval, signing, and confirmation."""
    evm_exec = _try_import("evm_executor")
    if evm_exec is None:
        log.error("evm_executor unavailable — cannot execute Base trade for %s", symbol)
        return False

    private_key = os.getenv("EVM_WALLET_PRIVATE_KEY", "").strip()
    if not private_key:
        log.warning("EVM_WALLET_PRIVATE_KEY not configured — skipping Base execution for %s", symbol)
        return False

    try:
        USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        usdc_amount = int(position_usd * 1_000_000)  # USDC has 6 decimals

        result = evm_exec.execute_swap("base", USDC_BASE, token_addr, usdc_amount, private_key)

        if result["success"]:
            log.info("Base execution confirmed: %s $%.2f tx=%s out=%d",
                     symbol, position_usd, result["tx_hash"], result["amount_out"])
            return True
        else:
            log.warning("Base execution failed for %s: %s", symbol, result.get("error", "unknown"))
            return False

    except Exception as exc:
        log.error("Base execution failed for %s: %s", symbol, exc)
        return False

def _execute_cex(symbol: str, position_usd: float) -> bool:
    """
    Execute a market buy on Robinhood Crypto.

    Called when chain="cex" — used for CEX-listed tokens (BTC, ETH, SOL, DOGE, etc.)
    that are not executed via a DEX route.

    Requires:
        ROBINHOOD_ENABLED=true
        ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD in .env

    Returns True on successful order placement. Does not wait for fill confirmation —
    Robinhood market orders fill asynchronously and are tracked via get_open_orders().
    """
    rh = _try_import("robinhood_connector")
    if rh is None:
        log.error("robinhood_connector unavailable — cannot execute CEX trade for %s", symbol)
        return False

    enabled = os.getenv("ROBINHOOD_ENABLED", "false").lower() == "true"
    if not enabled:
        log.info("_execute_cex: ROBINHOOD_ENABLED=false — skipping %s", symbol)
        return False

    api_key = os.getenv("ROBINHOOD_API_KEY", "").strip()
    if not api_key:
        log.warning("ROBINHOOD_API_KEY not configured — skipping CEX execution for %s", symbol)
        return False

    try:
        result = rh.place_crypto_order(
            symbol=symbol.upper(),
            side="buy",
            amount_usd=position_usd,
            order_type="market",
        )
        if result.get("status") == "ok":
            log.info(
                "Robinhood execution SUCCESS: %s $%.2f order_id=%s state=%s",
                symbol, position_usd, result.get("order_id"), result.get("state"),
            )
            return True
        else:
            log.warning(
                "Robinhood execution did not succeed: %s — %s",
                symbol, result.get("message", result.get("status")),
            )
            return False
    except Exception as exc:
        log.error("CEX execution failed for %s: %s", symbol, exc)
        return False
