# Breadbot — Claude Code Task List
# March 20, 2026

You are working on Breadbot, a commercial crypto trading bot.
Working directory: /Users/adrez/Desktop/cryptobot/deploy_repo/
All changes go to files in this directory only.
Never modify .env or credentials. Never print credential values.
Read files before editing. Use surgical edits — change only what each task requires.
After all tasks, run: git add -A && git commit -m "Claude Code: automode, status, exchange executor, composite" and report the commit hash.

---

## TASK 1 — Add /automode and /status Telegram commands to scanner.py

### Context
scanner.py handles all Telegram commands in `_handle_message()`.
auto_executor.py has a `get_strategy_summary()` method that returns current config.
The bot needs `/automode on` and `/automode off` commands so operators can toggle
execution mode without touching .env.

### What to add

#### 1A. In `_handle_message()` in scanner.py, add two new command branches:

```python
elif cmd == "automode":
    await handle_automode_command(client, args)
elif cmd == "status":
    await handle_status_command(client)
```

#### 1B. Add `handle_automode_command()` function to scanner.py:

```python
async def handle_automode_command(client: httpx.AsyncClient, args: str) -> None:
    """Handle /automode on|off — toggle auto-execution mode at runtime."""
    arg = args.strip().lower()
    if arg not in ("on", "off"):
        await send_message(client, "Usage: /automode on  or  /automode off")
        return
    # Write to bot_config table (DB-first config pattern used by AutoExecutor)
    conn = db_rw()
    try:
        conn.execute(
            """INSERT INTO bot_config (key, value, updated_at)
               VALUES ('execution_mode', ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            ("auto" if arg == "on" else "manual",)
        )
        conn.commit()
    finally:
        conn.close()
    mode_label = "AUTO" if arg == "on" else "MANUAL"
    await send_message(
        client,
        f"Execution mode set to <b>{mode_label}</b>.\n\n"
        f"{'Bot will auto-execute alerts that pass strategy thresholds.' if arg == 'on' else 'Bot will send alerts for manual approval.'}"
    )
    log.info("Execution mode changed to %s via Telegram", mode_label)
```

#### 1C. Add `handle_status_command()` function to scanner.py:

```python
async def handle_status_command(client: httpx.AsyncClient) -> None:
    """Handle /status — show full bot state snapshot."""
    summary = executor.get_strategy_summary()

    from alt_data_signals import get_cached_composite, get_cached_fear_greed, _cache as _alt_cache

    composite = get_cached_composite()
    fg = get_cached_fear_greed()
    fg_label = _alt_cache.get("fg_label", "")

    paused_str  = "PAUSED" if summary["is_paused"] else "ACTIVE"
    mode_str    = summary["execution_mode"].upper()
    strat_str   = summary["strategy"].upper()
    trades_str  = f"{summary['trades_today']}/{summary['max_trades_per_day']}"
    loss_str    = "LIMIT HIT" if summary["daily_loss_exceeded"] else "OK"

    comp_str = f"{composite:+.0f}" if composite is not None else "N/A"
    fg_str   = f"{fg}/100 ({fg_label})" if fg is not None else "N/A"

    text = (
        f"<b>Breadbot Status</b>\n\n"
        f"State:         {paused_str}\n"
        f"Mode:          {mode_str}\n"
        f"Strategy:      {strat_str}\n"
        f"Trades today:  {trades_str}\n"
        f"Daily loss:    {loss_str}\n\n"
        f"<b>Alt Data Signals</b>\n"
        f"Composite:     {comp_str}\n"
        f"Fear/Greed:    {fg_str}\n"
    )
    await send_message(client, text)
```

---

## TASK 2 — Add exchange_executor.py

Create a new file: `/Users/adrez/Desktop/cryptobot/deploy_repo/exchange_executor.py`

This module is the bridge between auto_executor.py (decides WHETHER to execute)
and the exchange connectors (Coinbase, Kraken, Solana). It is called from
process_pair() in scanner.py when result.executed is True.

It must:
- Be fully defensive — any exception logs and returns False, never raises
- Check EXECUTION_MODE=auto before attempting anything
- Route Solana tokens to solana_executor, Base tokens to evm_executor,
  CEX execution to coinbase/kraken connectors
- Log every attempt and outcome
- Return bool: True if executed, False if failed or skipped

```python
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
```

---

## TASK 3 — Wire exchange_executor into scanner.py process_pair()

In scanner.py, find the `process_pair()` function.
After the line `if result.executed:` block that sends the Telegram message,
add the actual exchange call.

Current code in process_pair (find this block):
```python
    if result.executed:
        msg = build_auto_buy_message(pair, score, flags, result)
        await send_message(client, msg)
```

Replace with:
```python
    if result.executed:
        # Attempt actual exchange execution
        trade_ok = False
        try:
            from exchange_executor import execute_trade
            trade_ok = execute_trade(
                chain=pair["chain"],
                token_addr=pair["token_addr"],
                symbol=pair.get("symbol", "UNKNOWN"),
                position_usd=result.position_usd,
                price_usd=pair.get("price_usd", 0.0),
            )
        except Exception as exc:
            log.error("exchange_executor import or call failed: %s", exc)

        # Telegram notice — adjust message if execution failed
        if not trade_ok:
            # Execution failed — downgrade to manual approval so operator can act
            update_alert_decision(alert_id, "pending")
            text, position = build_approval_message(pair, score, flags, result)
            text = f"[Auto-exec attempted but FAILED — manual approval needed]\n\n{text}"
            keyboard = build_buy_keyboard(alert_id, position)
            await send_message(client, text, reply_markup=keyboard)
        else:
            msg = build_auto_buy_message(pair, score, flags, result)
            await send_message(client, msg)
```

Also add this import at the top of scanner.py imports section (after existing imports):
The import is already done inline in process_pair above (lazy import pattern), so no
top-level import needed.

---

## TASK 4 — Syntax-check all modified files

After all edits, run from the deploy_repo directory:

```bash
cd /Users/adrez/Desktop/cryptobot/deploy_repo
python3 -m py_compile scanner.py && echo "scanner OK"
python3 -m py_compile exchange_executor.py && echo "exchange_executor OK"
```

Fix any syntax errors before committing.

---

## TASK 5 — Commit

```bash
cd /Users/adrez/Desktop/cryptobot/deploy_repo
git add scanner.py exchange_executor.py
git commit -m "Sprint 1A: exchange_executor, /automode, /status commands"
git push
```

Report the commit hash.

---

## DONE
When complete, output a summary in this exact format:
TASKS_COMPLETE
commit: <hash>
scanner.py: <OK or FAIL>
exchange_executor.py: <OK or FAIL>
syntax_check: <OK or FAIL>
notes: <any issues or deviations>
