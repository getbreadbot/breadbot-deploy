#!/usr/bin/env python3
"""
exchange_executor.py — Routes approved auto-execute orders to the correct connector.

Called from scanner.process_pair() when AutoExecutor.evaluate() returns executed=True.
This module does NOT make trade decisions — AutoExecutor does that.
This module only routes and logs.

Execution routing:
  chain=solana  → solana_executor.sign_and_send() (Jupiter V6 + Jito MEV)
  chain=base    → evm_executor.send_raw_transaction() (Uniswap V3 + Flashbots)
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
) -> bool:
    """
    Route and execute an auto-approved trade.

    Args:
        chain:        "solana" or "base"
        token_addr:   contract address of the token to buy
        symbol:       human-readable symbol (for logging)
        position_usd: dollar amount approved by AutoExecutor
        price_usd:    current price per token

    Returns:
        True if trade was submitted to the exchange successfully.
        False if execution was skipped, failed, or the connector is unconfigured.
    """
    mode = os.getenv("EXECUTION_MODE", "manual").strip().lower()
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
        # Check SOL balance for gas
        sol_balance = sol_exec.check_sol_balance()
        if sol_balance < 0.001:
            log.warning("Insufficient SOL for gas (%.6f SOL) — skipping %s", sol_balance, symbol)
            return False

        # Convert USD to lamports: 1 USDC = 1_000_000 lamports (6 decimals)
        usdc_amount_lamports = int(position_usd * 1_000_000)

        # Quote: USDC → target token
        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        quote = sol_exec.get_quote(USDC_MINT, token_addr, usdc_amount_lamports)

        # Build and send
        tx_b64 = sol_exec.build_swap_tx(quote)
        sig = sol_exec.sign_and_send(tx_b64)
        confirmed = sol_exec.confirm_tx(sig)

        if confirmed:
            log.info("Solana execution SUCCESS: %s $%.2f sig=%s", symbol, position_usd, sig)
        else:
            log.warning("Solana tx sent but not confirmed within timeout: %s sig=%s", symbol, sig)

        return confirmed

    except Exception as exc:
        log.error("Solana execution failed for %s: %s", symbol, exc)
        return False


def _execute_base(token_addr: str, symbol: str, position_usd: float, price_usd: float) -> bool:
    """Execute via Uniswap V3 on Base. MEV-protected via Flashbots when FLASHBOTS_PROTECT_ENABLED=true."""
    evm_exec = _try_import("evm_executor")
    if evm_exec is None:
        log.error("evm_executor unavailable — cannot execute Base trade for %s", symbol)
        return False

    wallet = os.getenv("EVM_WALLET_ADDRESS", "").strip()
    private_key = os.getenv("EVM_WALLET_PRIVATE_KEY", "").strip()
    if not wallet or not private_key:
        log.warning(
            "EVM_WALLET_ADDRESS or EVM_WALLET_PRIVATE_KEY not configured "
            "— skipping Base execution for %s", symbol
        )
        return False

    try:
        # Check ETH balance for gas
        eth_balance = evm_exec.get_eth_balance("base")
        if eth_balance < 0.0005:
            log.warning("Insufficient ETH for gas (%.6f ETH) — skipping %s", eth_balance, symbol)
            return False

        USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        usdc_amount = int(position_usd * 1_000_000)  # USDC has 6 decimals

        # Get quote
        quote = evm_exec.get_quote("base", USDC_BASE, token_addr, usdc_amount)
        if not quote or not quote.get("amount_out"):
            log.warning("No valid quote for %s on Base — skipping", symbol)
            return False

        # Apply slippage to min output
        slippage = int(os.getenv("EVM_MAX_SLIPPAGE_BPS", "50"))
        min_out = int(quote["amount_out"] * (1 - slippage / 10000))

        # Build swap tx (unsigned)
        tx = evm_exec.build_swap_tx("base", USDC_BASE, token_addr, usdc_amount, min_out)

        # Sign with private key
        try:
            from eth_account import Account  # type: ignore
            signed = Account.sign_transaction(tx, private_key)
            signed_hex = signed.rawTransaction.hex()
        except ImportError:
            log.error("eth-account not installed — cannot sign Base transaction")
            return False

        # Broadcast (routes through Flashbots Protect if enabled)
        tx_hash = evm_exec.send_raw_transaction("base", signed_hex)
        log.info("Base execution submitted: %s $%.2f tx=%s", symbol, position_usd, tx_hash)
        return True

    except Exception as exc:
        log.error("Base execution failed for %s: %s", symbol, exc)
        return False
