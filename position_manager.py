"""
position_manager.py — Session 49

Polls open positions every POSITION_CHECK_INTERVAL_SECONDS (default 60) and
exits them when stop-loss, take_profit_25, or take_profit_50 price levels
are breached.

Exit logic (Solana only for now — Base will follow once live-tested):
  - price <= stop_loss_usd     → SELL 100% (hard stop)
  - price >= take_profit_50    → SELL 100% of remaining (final exit)
  - price >= take_profit_25    → SELL 50% (partial exit, raises floor)

Partial exits are tracked in-memory via the exit_stage field. Once a
position has done its TP25 partial, only TP50 or SL can close the rest.

Env vars:
  POSITION_MANAGER_ENABLED         default "true"
  POSITION_CHECK_INTERVAL_SECONDS  default "60"
  POSITION_SELL_SLIPPAGE_BPS       default "500" (5% — memecoins need room)
  POSITION_TP25_SELL_FRACTION      default "0.5"

Telegram:
  /sell_now <position_id>          force-sell a position immediately (100%)

Database:
  Reads  positions WHERE status='open'
  Writes positions.status = 'closed' on full exit, 'open_partial' on TP25 partial

The module uses solana_executor directly — same path proven in Session 49
with the M2M manual sell (tx 2RopfgiSF1sP46wCk7QhVBLc5NaigrDCMPTUEQqkSE7Ln2QwpXtZStQNzFCLVGu5bs2BcApA5x9da7F5Ht83fbh8).
"""

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"
USDC_MINT_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_ADDRESS_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# In-memory tracker for which positions have taken their TP25 partial already.
# Keyed by position id. Cleared when position closes.
_partial_taken: dict[int, bool] = {}

# Simple cooldown to avoid hammering the same position if it oscillates
# around a trigger level or if a sell fails and the next poll retries.
# Keyed by position id → timestamp of last action.
_last_action_ts: dict[int, float] = {}
_ACTION_COOLDOWN_SECONDS = 120


def _cfg_float(key: str, default: str) -> float:
    try:
        return float(os.getenv(key, default))
    except ValueError:
        return float(default)


def _cfg_int(key: str, default: str) -> int:
    try:
        return int(os.getenv(key, default))
    except ValueError:
        return int(default)


def _cfg_bool(key: str, default: str) -> bool:
    return os.getenv(key, default).strip().lower() in ("true", "1", "yes", "on")


def _db_ro():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)


def _db_rw():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.execute("PRAGMA journal_mode=WAL;")
    return c


def _fetch_price_usd(chain: str, token_addr: str) -> float | None:
    """Best-effort current price via DEXScreener. Returns None if no pairs."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=10,
        )
        pairs = (r.json() or {}).get("pairs") or []
        if chain:
            chain_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == chain.lower()]
            pairs = chain_pairs or pairs
        if not pairs:
            return None
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        px = best.get("priceUsd")
        return float(px) if px else None
    except Exception as exc:
        log.debug("dexscreener price lookup failed for %s: %s", token_addr, exc)
        return None


def _get_solana_balance_raw(mint: str) -> int:
    """On-chain authoritative balance (raw integer) for the bot wallet's ATA of this mint."""
    rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    owner = os.getenv("SOLANA_WALLET_PUBKEY", "").strip()
    if not owner:
        return 0
    try:
        r = requests.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}]
        }, timeout=15)
        accts = ((r.json() or {}).get("result") or {}).get("value") or []
        if not accts:
            return 0
        info = (((accts[0].get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        ta = info.get("tokenAmount") or {}
        return int(ta.get("amount", 0))
    except Exception as exc:
        log.debug("wallet balance lookup failed for %s: %s", mint, exc)
        return 0


def _sell_solana(token_addr: str, sell_raw: int, symbol: str, slippage_bps: int) -> tuple[bool, float]:
    """
    Execute a token→USDC sell via solana_executor.
    Returns (success, usdc_received_ui).
    """
    try:
        import solana_executor as se
    except ImportError as exc:
        log.error("solana_executor not importable: %s", exc)
        return False, 0.0

    try:
        # Gas guard
        sol = se.check_sol_balance()
        if sol < 0.001:
            log.warning("Insufficient SOL for gas (%.6f) — cannot sell %s", sol, symbol)
            return False, 0.0

        quote = se.get_quote(token_addr, USDC_MINT_SOLANA, sell_raw, slippage_bps=slippage_bps)
        expected_usdc = int(quote.get("outAmount", 0)) / 1_000_000
        if expected_usdc <= 0:
            log.warning("Zero expected out for %s — aborting sell", symbol)
            return False, 0.0

        tx_b64 = se.build_swap_tx(quote)
        sig = se.sign_and_send(tx_b64)
        log.info("Position exit: %s sig=%s expected=$%.4f", symbol, sig, expected_usdc)
        confirmed = se.confirm_tx(sig, max_retries=20, poll_seconds=2.0)
        if confirmed:
            return True, expected_usdc
        log.warning("Sell tx sent but not confirmed: %s sig=%s", symbol, sig)
        return False, 0.0
    except Exception as exc:
        log.error("Sell failed for %s: %s", symbol, exc, exc_info=True)
        return False, 0.0


def _get_base_balance_raw(token_address: str) -> int:
    """On-chain authoritative balance (raw integer) for the bot's Base EVM wallet."""
    try:
        import evm_executor as ee
        return int(ee.get_token_balance("base", token_address))
    except Exception as exc:
        log.debug("Base balance lookup failed for %s: %s", token_address, exc)
        return 0


def _sell_base(token_addr: str, sell_raw: int, symbol: str, slippage_bps: int) -> tuple[bool, float]:
    """
    Execute a token->USDC sell via evm_executor on Base.
    Returns (success, usdc_received_ui).

    Note: evm_executor reads its own slippage from EVM_MAX_SLIPPAGE_BPS env var.
    slippage_bps is accepted for signature parity with _sell_solana but not forwarded
    (Odos quote flow handles slippage internally per its env setting).
    """
    try:
        import evm_executor as ee
    except ImportError as exc:
        log.error("evm_executor not importable: %s", exc)
        return False, 0.0

    private_key = os.getenv("EVM_WALLET_PRIVATE_KEY", "").strip()
    if not private_key:
        log.error("EVM_WALLET_PRIVATE_KEY missing - cannot sell %s on Base", symbol)
        return False, 0.0

    try:
        # Gas guard - Odos swaps on Base typically burn ~0.00004 ETH
        eth = ee.get_eth_balance("base")
        if eth < 0.0003:
            log.warning("Insufficient ETH for gas on Base (%.6f) - cannot sell %s", eth, symbol)
            return False, 0.0

        result = ee.execute_swap("base", token_addr, USDC_ADDRESS_BASE, sell_raw, private_key)
        if not result.get("success"):
            log.warning("Base sell failed for %s: %s", symbol, result.get("error", "unknown"))
            return False, 0.0

        usdc_received = int(result.get("amount_out", 0)) / 1_000_000
        log.info("Position exit (Base): %s tx=%s expected=$%.4f",
                 symbol, result.get("tx_hash"), usdc_received)
        return True, usdc_received
    except Exception as exc:
        log.error("Base sell failed for %s: %s", symbol, exc, exc_info=True)
        return False, 0.0


def _mark_closed(position_id: int, note: str = "") -> None:
    conn = _db_rw()
    try:
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=datetime('now') WHERE id=?",
            (position_id,),
        )
        conn.commit()
    finally:
        conn.close()
    _partial_taken.pop(position_id, None)
    _last_action_ts.pop(position_id, None)
    log.info("Position #%d marked closed%s", position_id, f" ({note})" if note else "")


def _send_telegram(text: str) -> None:
    """Best-effort Telegram notification. Silent if not configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def _evaluate_position(row: dict) -> None:
    """Check one open position against its SL/TP levels and act if breached."""
    pid = row["id"]
    symbol = row["symbol"]
    chain = row["chain"]
    token_addr = row["token_addr"]
    entry = float(row["entry_price"] or 0)
    qty_db = float(row["quantity"] or 0)
    cost = float(row["cost_basis_usd"] or 0)
    sl = float(row["stop_loss_usd"] or 0)
    tp25 = float(row["take_profit_25"] or 0)
    tp50 = float(row["take_profit_50"] or 0)

    if entry <= 0 or qty_db <= 0:
        log.debug("Position #%d has zero entry/qty — skipping", pid)
        return

    # Cooldown check — don't retry within cooldown window
    now = time.time()
    last = _last_action_ts.get(pid, 0)
    if now - last < _ACTION_COOLDOWN_SECONDS:
        return

    # Chain dispatch - Solana and Base are live-tested (S49, S50).
    if chain not in ("solana", "base"):
        log.debug("Position #%d chain=%s unsupported by position_manager - skipping", pid, chain)
        return

    price = _fetch_price_usd(chain, token_addr)
    if price is None:
        log.debug("Position #%d %s: no price available", pid, symbol)
        return

    pct_vs_entry = ((price - entry) / entry) * 100
    partial_done = _partial_taken.get(pid, False)

    # Decision tree — order matters: hard stop-loss wins
    if price <= sl:
        action = "SL"
        sell_fraction = 1.0
    elif price >= tp50:
        action = "TP50"
        sell_fraction = 1.0
    elif price >= tp25 and not partial_done:
        action = "TP25"
        sell_fraction = _cfg_float("POSITION_TP25_SELL_FRACTION", "0.5")
    else:
        # Nothing to do — log at debug for visibility
        log.debug(
            "Position #%d %s: price=$%.9f entry=$%.9f (%+.1f%%) SL=$%.9f TP25=$%.9f TP50=$%.9f — hold",
            pid, symbol, price, entry, pct_vs_entry, sl, tp25, tp50,
        )
        return

    # Lookup authoritative on-chain balance (qty_db can diverge — memecoins with tax, etc.)
    if chain == "solana":
        wallet_raw = _get_solana_balance_raw(token_addr)
    elif chain == "base":
        wallet_raw = _get_base_balance_raw(token_addr)
    else:
        log.debug("Position #%d chain=%s has no balance lookup - skipping", pid, chain)
        return
    if wallet_raw <= 0:
        log.warning(
            "Position #%d %s: %s triggered but wallet holds 0 of this mint — marking closed as dust",
            pid, symbol, action,
        )
        _mark_closed(pid, note=f"{action}, zero wallet balance")
        return

    sell_raw = int(wallet_raw * sell_fraction) if sell_fraction < 1.0 else wallet_raw
    if sell_raw <= 0:
        log.debug("Position #%d %s: computed sell_raw=0 — skipping", pid, symbol)
        return

    slippage = _cfg_int("POSITION_SELL_SLIPPAGE_BPS", "500")
    log.info(
        "Position #%d %s: %s triggered — price=$%.9f entry=$%.9f (%+.1f%%) | selling %.1f%% (raw=%d)",
        pid, symbol, action, price, entry, pct_vs_entry, sell_fraction * 100, sell_raw,
    )

    _last_action_ts[pid] = now
    if chain == "solana":
        success, usdc_out = _sell_solana(token_addr, sell_raw, symbol, slippage)
    else:  # base
        success, usdc_out = _sell_base(token_addr, sell_raw, symbol, slippage)

    if not success:
        log.warning("Position #%d %s: %s sell did not confirm — will retry after cooldown", pid, symbol, action)
        _send_telegram(
            f"⚠️ Position Manager: {action} trigger failed for {symbol} (#{pid})\n"
            f"price=${price:.9f} entry=${entry:.9f} ({pct_vs_entry:+.1f}%)\n"
            f"Will retry in {_ACTION_COOLDOWN_SECONDS}s. Inspect logs."
        )
        return

    realized_pnl = (usdc_out if sell_fraction == 1.0 else usdc_out) - (cost * sell_fraction)
    _send_telegram(
        f"🔔 *Position Manager*: {action} exit — {symbol} (#{pid})\n"
        f"price=${price:.9f} entry=${entry:.9f} ({pct_vs_entry:+.1f}%)\n"
        f"sold {sell_fraction*100:.0f}% → ${usdc_out:.2f} USDC\n"
        f"realized vs pro-rata cost: ${realized_pnl:+.2f}"
    )

    if action == "TP25" and sell_fraction < 1.0:
        _partial_taken[pid] = True
        log.info("Position #%d %s: TP25 partial complete, position remains open for TP50/SL", pid, symbol)
    else:
        _mark_closed(pid, note=action)


def _load_open_positions() -> list[dict]:
    conn = _db_ro()
    try:
        cur = conn.execute(
            """SELECT id, chain, token_addr, token_name, symbol, entry_price,
                      quantity, cost_basis_usd, stop_loss_usd, take_profit_25,
                      take_profit_50, exchange, opened_at
               FROM positions WHERE status='open'"""
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


async def position_manager_loop() -> None:
    """Main coroutine — runs for the life of the bot process."""
    if not _cfg_bool("POSITION_MANAGER_ENABLED", "true"):
        log.info("position_manager disabled via POSITION_MANAGER_ENABLED")
        return

    interval = _cfg_int("POSITION_CHECK_INTERVAL_SECONDS", "60")
    log.info("position_manager: starting (interval=%ds, chains=solana+base)", interval)

    while True:
        try:
            positions = _load_open_positions()
            if positions:
                log.debug("position_manager: %d open positions", len(positions))
                for row in positions:
                    _evaluate_position(row)
        except Exception as exc:
            log.error("position_manager loop error: %s", exc, exc_info=True)

        await asyncio.sleep(interval)


# ── Self-test (read-only, no execution) ───────────────────────────────────────
# This block gets appended to position_manager.py just before the __main__ guard.

def force_sell_position(position_id: int) -> dict:
    '''
    Force-sell 100% of a single open position by ID.

    Returns:
        {
          'success': bool,
          'position_id': int,
          'symbol': str | None,
          'chain': str | None,
          'usdc_received': float,
          'cost_basis': float,
          'realized_pnl': float,
          'error': str | None,
        }
    '''
    positions = _load_open_positions()
    match = next((p for p in positions if int(p['id']) == int(position_id)), None)

    if match is None:
        return {
            'success': False,
            'position_id': int(position_id),
            'symbol': None,
            'chain': None,
            'usdc_received': 0.0,
            'cost_basis': 0.0,
            'realized_pnl': 0.0,
            'error': f'position #{position_id} not found or not open',
        }

    chain = (match.get('chain') or '').lower()
    symbol = match.get('symbol') or match.get('token_name') or '?'
    token_addr = match.get('token_addr')
    cost = float(match.get('cost_basis_usd') or 0)
    slippage = _cfg_int('POSITION_MANAGER_SLIPPAGE_BPS', '150')

    if chain == 'solana':
        sell_raw = _get_solana_balance_raw(token_addr)
        if sell_raw <= 0:
            return {'success': False, 'position_id': int(position_id),
                    'symbol': symbol, 'chain': chain, 'usdc_received': 0.0,
                    'cost_basis': cost, 'realized_pnl': 0.0,
                    'error': 'zero token balance on-chain'}
        success, usdc_out = _sell_solana(token_addr, sell_raw, symbol, slippage)
    elif chain == 'base':
        sell_raw = _get_base_balance_raw(token_addr)
        if sell_raw <= 0:
            return {'success': False, 'position_id': int(position_id),
                    'symbol': symbol, 'chain': chain, 'usdc_received': 0.0,
                    'cost_basis': cost, 'realized_pnl': 0.0,
                    'error': 'zero token balance on-chain'}
        success, usdc_out = _sell_base(token_addr, sell_raw, symbol, slippage)
    else:
        return {'success': False, 'position_id': int(position_id),
                'symbol': symbol, 'chain': chain, 'usdc_received': 0.0,
                'cost_basis': cost, 'realized_pnl': 0.0,
                'error': f'unsupported chain: {chain!r}'}

    if not success:
        return {'success': False, 'position_id': int(position_id),
                'symbol': symbol, 'chain': chain, 'usdc_received': 0.0,
                'cost_basis': cost, 'realized_pnl': 0.0,
                'error': f'{chain} sell did not confirm'}

    realized = float(usdc_out) - cost
    _mark_closed(int(position_id), note='SELL_NOW')
    return {
        'success': True,
        'position_id': int(position_id),
        'symbol': symbol,
        'chain': chain,
        'usdc_received': float(usdc_out),
        'cost_basis': cost,
        'realized_pnl': realized,
        'error': None,
    }
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s  %(message)s")
    print("position_manager self-test — dry-run, no sells will execute")
    open_positions = _load_open_positions()
    print(f"open positions: {len(open_positions)}")
    for row in open_positions:
        addr = row["token_addr"]
        chain = row["chain"]
        price = _fetch_price_usd(chain, addr)
        entry = float(row["entry_price"] or 0)
        sl = float(row["stop_loss_usd"] or 0)
        tp25 = float(row["take_profit_25"] or 0)
        tp50 = float(row["take_profit_50"] or 0)
        pct = ((price - entry) / entry) * 100 if price and entry else 0
        print(f"  #{row['id']} {row['symbol']:<10} {chain}  entry=${entry:.9f}  price=${price:.9f} ({pct:+.1f}%)")
        print(f"     SL=${sl:.9f}  TP25=${tp25:.9f}  TP50=${tp50:.9f}")
        if price is None:
            print(f"     → no price data")
        elif price <= sl:
            print(f"     → WOULD SL (100% exit)")
        elif price >= tp50:
            print(f"     → WOULD TP50 (100% exit)")
        elif price >= tp25:
            print(f"     → WOULD TP25 (50% partial)")
        else:
            print(f"     → HOLD")
