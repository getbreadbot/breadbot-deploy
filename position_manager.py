"""
position_manager.py — managed exits for open positions

Polls open positions every POSITION_CHECK_INTERVAL_SECONDS (default 60) and
exits them when stop-loss, take_profit_25, or take_profit_50 price levels
are breached. Solana via Jupiter+Jito; Base via Odos (added S50+).

Exit logic:
  - price <= stop_loss_usd     → SELL 100% (hard stop)
  - price >= take_profit_50    → SELL 100% of remaining (final exit)
  - price >= take_profit_25    → SELL 50% (partial exit, raises floor)

Partial exits tracked in-memory via _partial_taken. Once a position has
taken its TP25 partial, only TP50 or SL can close the rest.

──────────────────────────────────────────────────────────────────────────
S55 P0 — daily_summary write hook (critical):
──────────────────────────────────────────────────────────────────────────
_mark_closed() accepts realized_pnl and UPSERTs today's row in
daily_summary in the same transaction as the positions UPDATE. This is
what arms the auto_executor daily loss circuit breaker.

Regression history: between mid-March and April 20, 2026 the write hook
was absent while auto_executor._daily_loss_exceeded() kept reading the
(stale) table. The cap was cosmetic. Do NOT remove the daily_summary
UPSERT from _mark_closed without also removing the reader.

All three close paths MUST pass realized_pnl:
  - SL/TP via _evaluate_position        → realized_pnl = usdc_out - cost
  - Zero-wallet dust close              → realized_pnl = -cost
  - /sell_now manual close              → realized_pnl = usdc_out - cost

──────────────────────────────────────────────────────────────────────────
S55 P3 — dynamic slippage escalation on SL retry (critical):
──────────────────────────────────────────────────────────────────────────
When a sell fails with SLIPPAGE (Jupiter program error 6001, detected
by solana_executor.confirm_tx), _evaluate_position retries IMMEDIATELY
with the next tier in the schedule. Schedule:
  sorted({POSITION_SELL_SLIPPAGE_BPS, 1500, 3000} where >= base)
  default: [500, 1500, 3000]  (5%, 15%, 30%)

Non-SLIPPAGE failures (TIMEOUT, FAILED, NO_GAS, etc.) still serve the
120s _ACTION_COOLDOWN_SECONDS — retrying with more slippage wouldn't
help those, and hammering a failing RPC is counterproductive.

Regression history: S54 vibetrading dumped -36% → -76% in the 2 min
cooldown window after a slippage-rejected SL tx. ~$12 extra loss.
Do NOT collapse this back to "treat all errors the same" — the err_code
dispatch is load-bearing.

──────────────────────────────────────────────────────────────────────────
Env vars:
  POSITION_MANAGER_ENABLED         default "true"
  POSITION_CHECK_INTERVAL_SECONDS  default "60"
  POSITION_SELL_SLIPPAGE_BPS       default "500" (base of the escalation schedule)
  POSITION_TP25_SELL_FRACTION      default "0.5"

Telegram:
  /sell_now <position_id>          force-sell a position immediately (100%)
                                   NOTE: sell_now does NOT use the retry
                                   escalation — it runs a single attempt
                                   at the configured slippage.

Database:
  Reads  positions WHERE status='open'
  Writes positions.status (closed / open_partial)
         daily_summary   UPSERT today's realized_pnl + trades_count

Origin tx (Session 49 validation):
  2RopfgiSF1sP46wCk7QhVBLc5NaigrDCMPTUEQqkSE7Ln2QwpXtZStQNzFCLVGu5bs2BcApA5x9da7F5Ht83fbh8
"""

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
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


def _bc_str(config_key: str, env_key: str, default: str = "") -> str:
    """Read from bot_config table, fall back to env, then default."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        row = conn.execute("SELECT value FROM bot_config WHERE key=?", (config_key,)).fetchone()
        conn.close()
        if row and row[0] is not None:
            return str(row[0])
    except Exception:
        pass
    return os.getenv(env_key, default)


def _bc_float(config_key: str, env_key: str, default: float = 0.0) -> float:
    """Read from bot_config table, fall back to env, then default."""
    val = _bc_str(config_key, env_key, str(default))
    try:
        return float(val)
    except ValueError:
        return default


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


def _sell_solana(
    token_addr: str,
    sell_raw: int,
    symbol: str,
    slippage_bps: int,
    min_acceptable_usdc: float | None = None,
) -> tuple[bool, float, str | None]:
    """
    Execute a token→USDC sell via solana_executor.
    Returns (success, usdc_received_ui, err_code).

    S80 P3b: when min_acceptable_usdc is set, the Jupiter quote outAmount
    is compared against this floor. If the quote returns less than the floor,
    the function returns (False, 0.0, "QUOTE_BLOWN") without submitting the
    transaction. Caller decides whether to escalate or accept on retry.
    """
    try:
        import solana_executor as se
    except ImportError as exc:
        log.error("solana_executor not importable: %s", exc)
        return False, 0.0, "IMPORT_ERROR"

    try:
        # Gas guard
        sol = se.check_sol_balance()
        if sol < 0.001:
            log.warning("Insufficient SOL for gas (%.6f) — cannot sell %s", sol, symbol)
            return False, 0.0, "NO_GAS"

        quote = se.get_quote(token_addr, USDC_MINT_SOLANA, sell_raw, slippage_bps=slippage_bps)
        expected_usdc = int(quote.get("outAmount", 0)) / 1_000_000
        if expected_usdc <= 0:
            log.warning("Zero expected out for %s — aborting sell", symbol)
            return False, 0.0, "ZERO_QUOTE"

        # S80 P3b: quote sanity guard — refuse to submit if the quote is
        # materially worse than what the observed price implies. Caller may
        # retry at a higher slippage tier or accept on the second pass.
        if min_acceptable_usdc is not None and expected_usdc < min_acceptable_usdc:
            gap_pct = (1 - expected_usdc / min_acceptable_usdc) * 100
            log.warning(
                "QUOTE_BLOWN: %s expected=$%.4f floor=$%.4f gap=%.1f%% (slippage=%dbps) — refusing submit",
                symbol, expected_usdc, min_acceptable_usdc, gap_pct, slippage_bps,
            )
            return False, 0.0, "QUOTE_BLOWN"

        tx_b64 = se.build_swap_tx(quote)
        sig = se.sign_and_send(tx_b64)
        log.info("Position exit: %s sig=%s expected=$%.4f (slippage=%dbps)",
                 symbol, sig, expected_usdc, slippage_bps)
        confirmed, err_code = se.confirm_tx(sig, max_retries=20, poll_seconds=2.0)
        if confirmed:
            # S58 P0: reclaim ATA rent for this specific token after a
            # successful sell. Best-effort — if it fails, the sell still
            # succeeded and the next sweep (manual close_dead_atas or the
            # next sell on the same mint) will reclaim it.
            try:
                closed = se.close_empty_atas(mint_filter=token_addr)
                if closed:
                    log.info("ATA_RECLAIM: closed %d empty ATA(s) for %s after sell", closed, symbol)
            except Exception as exc:
                log.debug("ATA_RECLAIM best-effort failed for %s: %s", symbol, exc)
            return True, expected_usdc, None
        log.warning("Sell tx sent but not confirmed (%s): %s sig=%s", err_code, symbol, sig)
        return False, 0.0, err_code
    except Exception as exc:
        log.error("Sell failed for %s: %s", symbol, exc, exc_info=True)
        # S60 P2: classify tx-size failures distinctly. When slippage escalates
        # to 1500bps, Jupiter may return a multi-hop route whose serialized tx
        # exceeds Solana's max (~1644 bytes). Both Jito and standard RPC reject
        # these with "base64 encoded too large" or "could not be decoded".
        # These are NOT terminal — a higher-slippage tier often produces a
        # different, smaller route. Return TX_TOO_LARGE so the escalator
        # continues rather than falling to 2-min cooldown.
        msg = str(exc).lower()
        if (
            "base64 encoded too large" in msg
            or "could not be decoded" in msg
            or "transaction too large" in msg
        ):
            return False, 0.0, "TX_TOO_LARGE"
        return False, 0.0, "EXCEPTION"


def _get_base_balance_raw(token_address: str) -> int:
    """On-chain authoritative balance (raw integer) for the bot's Base EVM wallet."""
    try:
        import evm_executor as ee
        return int(ee.get_token_balance("base", token_address))
    except Exception as exc:
        log.debug("Base balance lookup failed for %s: %s", token_address, exc)
        return 0


def _sell_base(token_addr: str, sell_raw: int, symbol: str, slippage_bps: int) -> tuple[bool, float, str | None]:
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
        return False, 0.0, "IMPORT_ERROR"

    private_key = os.getenv("EVM_WALLET_PRIVATE_KEY", "").strip()
    if not private_key:
        log.error("EVM_WALLET_PRIVATE_KEY missing - cannot sell %s on Base", symbol)
        return False, 0.0, "NO_KEY"

    try:
        # Gas guard - Odos swaps on Base typically burn ~0.00004 ETH
        eth = ee.get_eth_balance("base")
        if eth < 0.0003:
            log.warning("Insufficient ETH for gas on Base (%.6f) - cannot sell %s", eth, symbol)
            return False, 0.0, "NO_GAS"

        result = ee.execute_swap("base", token_addr, USDC_ADDRESS_BASE, sell_raw, private_key)
        if not result.get("success"):
            log.warning("Base sell failed for %s: %s", symbol, result.get("error", "unknown"))
            return False, 0.0, "FAILED"

        usdc_received = int(result.get("amount_out", 0)) / 1_000_000
        log.info("Position exit (Base): %s tx=%s expected=$%.4f",
                 symbol, result.get("tx_hash"), usdc_received)
        return True, usdc_received, None
    except Exception as exc:
        log.error("Base sell failed for %s: %s", symbol, exc, exc_info=True)
        return False, 0.0, "EXCEPTION"


def _record_partial_realization(position_id: int, partial_pnl: float) -> None:
    """S63 P1: book a TP25 partial gain/loss without closing the position.

    Adds `partial_pnl` to positions.realized_pnl_usd (accumulating, not
    replacing) and to today's daily_summary row. Position status stays 'open'.
    trades_count is NOT incremented — the close will do that, so one round-trip
    counts as one trade in the summary.
    """
    conn = _db_rw()
    try:
        conn.execute(
            """UPDATE positions
                  SET realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?
                WHERE id=?""",
            (float(partial_pnl), position_id),
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn.execute(
            """INSERT INTO daily_summary (date, realized_pnl, trades_count)
               VALUES (?, ?, 0)
               ON CONFLICT(date) DO UPDATE SET
                   realized_pnl = realized_pnl + excluded.realized_pnl""",
            (today, float(partial_pnl)),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_closed(position_id: int, note: str = "", realized_pnl: float | None = None, exit_price: float | None = None) -> None:
    """Mark position closed and upsert realized_pnl into daily_summary atomically.

    S55 P0: daily_summary had no writer since mid-March, disabling the
    daily loss circuit breaker. realized_pnl is written in the same
    transaction as the positions UPDATE so they cannot drift.

    S63 P1: realized_pnl passed in here is the CLOSING LEG only (whole position
    if no prior TP25, or remaining 50% if TP25 already fired). It is *added*
    to positions.realized_pnl_usd rather than overwriting, so a TP25 partial
    previously booked via _record_partial_realization is preserved.
    """
    conn = _db_rw()
    try:
        conn.execute(
            """UPDATE positions
                  SET status='closed',
                      closed_at=datetime('now'),
                      realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + COALESCE(?, 0),
                      exit_price       = COALESCE(?, exit_price)
                WHERE id=?""",
            (realized_pnl, exit_price, position_id),
        )
        if realized_pnl is not None:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            conn.execute(
                """INSERT INTO daily_summary (date, realized_pnl, trades_count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(date) DO UPDATE SET
                       realized_pnl = realized_pnl + excluded.realized_pnl,
                       trades_count = trades_count + 1""",
                (today, float(realized_pnl)),
            )
        conn.commit()
    finally:
        conn.close()
    _partial_taken.pop(position_id, None)
    _last_action_ts.pop(position_id, None)
    pnl_str = f" pnl=${realized_pnl:+.2f}" if realized_pnl is not None else ""
    log.info("Position #%d marked closed%s%s", position_id, f" ({note})" if note else "", pnl_str)


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


# ── S74 P3: exit slippage instrumentation ───────────────────────────────────

def _log_exit_poll(
    position_id: int,
    *,
    observed_price: float | None,
    pct_vs_entry: float | None,
    age_sec: float | None,
    action_decided: str,
) -> None:
    """Append one row to exit_polls. Best-effort — never raises."""
    try:
        conn = _db_rw()
        try:
            conn.execute(
                """
                INSERT INTO exit_polls (
                    position_id, observed_price, pct_vs_entry, age_sec,
                    action_decided
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    observed_price,
                    pct_vs_entry,
                    int(age_sec) if age_sec is not None else None,
                    action_decided,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("exit_polls insert failed for pos=%s: %s", position_id, exc)


def _log_exit_attempt(
    position_id: int,
    *,
    action: str,
    retry_tier: int,
    requested_slippage_bps: int,
    observed_price: float,
    sl_price: float,
    tp25_price: float,
    tp50_price: float,
    pct_vs_entry: float,
    age_sec: float,
    quantity_sold: float,
    executed_usdc: float,
    success: bool,
    err_code: str | None,
    latency_ms: int,
) -> None:
    """Append one row to exit_attempts. Best-effort — never raises.

    Computes derived fields executed_price (= usdc/raw, only meaningful on
    success), slippage_pct_vs_observed (= executed - observed, signed),
    slippage_pct_vs_sl (= executed - sl, signed). Negative values mean we
    executed below the reference price; positive means above.
    """
    try:
        executed_price = (executed_usdc / quantity_sold) if (success and quantity_sold > 0) else None

        slip_vs_obs = None
        slip_vs_sl  = None
        if executed_price is not None:
            if observed_price and observed_price > 0:
                slip_vs_obs = ((executed_price - observed_price) / observed_price) * 100
            if sl_price and sl_price > 0:
                slip_vs_sl = ((executed_price - sl_price) / sl_price) * 100

        conn = _db_rw()
        try:
            conn.execute(
                """
                INSERT INTO exit_attempts (
                    position_id, action, retry_tier, requested_slippage_bps,
                    observed_price, sl_price, tp25_price, tp50_price,
                    pct_vs_entry, age_sec, quantity_sold, executed_usdc,
                    executed_price, slippage_pct_vs_observed, slippage_pct_vs_sl,
                    success, err_code, latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id, action, retry_tier, requested_slippage_bps,
                    observed_price, sl_price, tp25_price, tp50_price,
                    pct_vs_entry, int(age_sec), quantity_sold, executed_usdc,
                    executed_price, slip_vs_obs, slip_vs_sl,
                    1 if success else 0, err_code, latency_ms,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("exit_attempts insert failed for pos=%s: %s", position_id, exc)


def _time_stop_check(age_sec: float, pct_vs_entry: float) -> tuple[bool, str]:
    """S74 P1a: time-stop on losers.

    Loser-only by design: positive-territory positions are left to the TP
    ladder. Three-tier cascade evaluated against the current position age
    and percent-vs-entry:

      30 min + at or below TIME_STOP_30MIN_LOSS_PCT  (default -5.0%)
      60 min + at or below TIME_STOP_60MIN_LOSS_PCT  (default -2.0%)
      TIME_STOP_HARD_MINUTES + any loss              (default 120 min)

    Tunable via env or bot_config:
      TIME_STOP_ENABLED          — master switch, default "false"
      TIME_STOP_30MIN_LOSS_PCT   — default -5.0
      TIME_STOP_60MIN_LOSS_PCT   — default -2.0
      TIME_STOP_HARD_MINUTES     — default 120

    Returns (should_exit, reason). Reason is a short tag included in
    _mark_closed note for forensics.
    """
    if not _cfg_bool("TIME_STOP_ENABLED", "false"):
        return (False, "")
    if pct_vs_entry >= 0:
        # Winners and break-evens go through TPs, never time-stop.
        return (False, "")

    age_min = age_sec / 60.0
    hard_min = _cfg_float("TIME_STOP_HARD_MINUTES", "120")
    if age_min >= hard_min:
        return (True, f"hard_{int(hard_min)}min")

    if age_min >= 60:
        threshold_60 = _cfg_float("TIME_STOP_60MIN_LOSS_PCT", "-2.0")
        if pct_vs_entry <= threshold_60:
            return (True, f"60min_at_{pct_vs_entry:.1f}pct")

    if age_min >= 30:
        threshold_30 = _cfg_float("TIME_STOP_30MIN_LOSS_PCT", "-5.0")
        if pct_vs_entry <= threshold_30:
            return (True, f"30min_at_{pct_vs_entry:.1f}pct")

    return (False, "")


def _evaluate_position(row: dict) -> None:
    """Check one open position against its SL/TP levels and act if breached."""
    pid = row["id"]
    symbol = row["symbol"]
    token_name = row.get("token_name") or ""
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
        _log_exit_poll(pid, observed_price=None, pct_vs_entry=None,
                       age_sec=None, action_decided="cooldown")
        return

    # Chain dispatch - Solana and Base are live-tested (S49, S50).
    if chain not in ("solana", "base"):
        log.debug("Position #%d chain=%s unsupported by position_manager - skipping", pid, chain)
        _log_exit_poll(pid, observed_price=None, pct_vs_entry=None,
                       age_sec=None, action_decided="unsupported_chain")
        return

    price = _fetch_price_usd(chain, token_addr)
    if price is None:
        log.debug("Position #%d %s: no price available", pid, symbol)
        _log_exit_poll(pid, observed_price=None, pct_vs_entry=None,
                       age_sec=None, action_decided="no_price")
        return

    pct_vs_entry = ((price - entry) / entry) * 100
    _last_pct_cache[pid] = pct_vs_entry  # S80 P3a
    # S63 P0: source of truth for "TP25 already fired" is positions.realized_pnl_usd,
    # not the in-memory _partial_taken dict. Dict survives only within one bot
    # process; DB survives restarts. If realized_pnl_usd > 0 on an open position,
    # the TP25 leg has already been booked and only 50% of cost remains.
    already_realized = float(row.get("realized_pnl_usd") or 0)
    partial_done = already_realized > 0 or _partial_taken.get(pid, False)

    # S62 P1: SL arming delay. For the first 120 seconds after entry, skip SL
    # checks. Rationale: pullback-monitor buys execute into steep downdrafts
    # and the first 1-2 minutes are the highest-noise window. KITTY (-16% in
    # 2 min), GME (-38% in 5 min), and BRICKS (-36% in 60 seconds) all
    # stopped out immediately after entry, and all three went on to dump
    # 90+% afterward — confirming the stops were correctly placed, but also
    # that we entered into continued downside. Deferring SL arming for 120s
    # gives the position room to settle; TP25 and TP50 remain active so
    # upside capture is unaffected. If price is still below SL after 120s,
    # the normal flow resumes and exits.
    _SL_ARM_DELAY_SEC = 120
    try:
        from datetime import datetime, timezone
        opened_at_str = row["opened_at"]
        if opened_at_str:
            opened_dt = datetime.fromisoformat(opened_at_str.replace(" ", "T")).replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - opened_dt).total_seconds()
        else:
            age_sec = 1e9  # unknown opened_at — arm immediately, safer default
    except Exception:
        age_sec = 1e9  # parse failure — arm immediately
    sl_armed = age_sec >= _SL_ARM_DELAY_SEC
    if not sl_armed and price <= sl:
        log.info(
            "Position #%d %s: price=$%.9f below SL=$%.9f (%.1fs < %ds arming delay) — SUPPRESSING SL",
            pid, symbol, price, sl, age_sec, _SL_ARM_DELAY_SEC,
        )

    # S74 P1a: time-stop on losers (loser-only). Evaluated after SL but
    # before TPs — a position bleeding for 30+ minutes shouldn't ride a
    # late TP, the data shows losers held >3x longer than winners with
    # no recovery. Disabled by default; flip TIME_STOP_ENABLED=true to
    # activate. Wins remain on the TP ladder regardless of age.
    time_stop_hit, time_stop_reason = _time_stop_check(age_sec, pct_vs_entry)

    # S82 P5: Trailing stop — update trail_high and trail_stop on each poll.
    # Activation: once price rises >= trailing_stop_activation_pct above entry,
    # the trailing stop engages. trail_stop follows price upward at
    # trailing_stop_distance_pct below the running high.
    _trail_enabled = _bc_str("trailing_stop_enabled", "TRAILING_STOP_ENABLED", "true").lower() == "true"
    _trail_activation_pct = _bc_float("trailing_stop_activation_pct", "TRAILING_STOP_ACTIVATION_PCT", 10.0)
    _trail_distance_pct = _bc_float("trailing_stop_distance_pct", "TRAILING_STOP_DISTANCE_PCT", 8.0)

    trail_high_db = float(row.get("trail_high") or 0)
    trail_stop_db = float(row.get("trail_stop") or 0)
    trail_stop_active = trail_stop_db > 0

    if _trail_enabled and price > 0 and entry > 0:
        activation_price = entry * (1 + _trail_activation_pct / 100)
        if price >= activation_price:
            if price > trail_high_db:
                # New high — update trail_high and trail_stop
                new_trail_stop = price * (1 - _trail_distance_pct / 100)
                if new_trail_stop > trail_stop_db:
                    trail_high_db = price
                    trail_stop_db = new_trail_stop
                    trail_stop_active = True
                    try:
                        conn = db_rw()
                        conn.execute(
                            "UPDATE positions SET trail_high=?, trail_stop=? WHERE id=?",
                            (trail_high_db, trail_stop_db, pid),
                        )
                        conn.commit()
                    except Exception as e:
                        log.warning("Position #%d: trail_stop DB update failed: %s", pid, e)
                    log.info(
                        "Position #%d %s: new trail high $%.9f → trail_stop $%.9f (+%.1f%% from entry)",
                        pid, symbol, trail_high_db, trail_stop_db,
                        ((trail_stop_db - entry) / entry) * 100,
                    )

    # Decision tree — order matters: hard stop-loss wins (once armed)
    if sl_armed and price <= sl:
        action = "SL"
        sell_fraction = 1.0
    elif time_stop_hit:
        action = "TIME_STOP"
        sell_fraction = 1.0
        log.info(
            "Position #%d %s: time-stop fired (%s) — price=$%.9f entry=$%.9f (%+.1f%%) age=%.1fmin",
            pid, symbol, time_stop_reason, price, entry, pct_vs_entry, age_sec / 60.0,
        )
    elif trail_stop_active and price <= trail_stop_db:
        action = "TRAILING_STOP"
        sell_fraction = 1.0
        log.info(
            "Position #%d %s: TRAILING STOP fired — price=$%.9f trail_stop=$%.9f "
            "trail_high=$%.9f entry=$%.9f (locking in +%.1f%%)",
            pid, symbol, price, trail_stop_db, trail_high_db, entry,
            ((trail_stop_db - entry) / entry) * 100,
        )
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
        # S74 P3: also captures the SL-suppressed-by-arming case via a
        # distinct action_decided value, since price <= sl while !sl_armed
        # falls through to the "else" hold branch.
        decided = "sl_suppressed_arming" if (price <= sl and not sl_armed) else "hold"
        _log_exit_poll(pid, observed_price=price, pct_vs_entry=pct_vs_entry,
                       age_sec=age_sec, action_decided=decided)
        return

    # S82 P4: Grace period — skip wallet balance check for very new positions.
    # RPC can take 5-15s to index a new token account after a swap lands.
    # Without this guard, the dust-close path fires on stale zero-balance reads.
    BALANCE_GRACE_SEC = 45
    if age_sec < BALANCE_GRACE_SEC:
        log.info(
            "Position #%d %s: age %ds < %ds grace — skipping balance check, holding",
            pid, symbol, int(age_sec), BALANCE_GRACE_SEC,
        )
        _log_exit_poll(pid, observed_price=price, pct_vs_entry=pct_vs_entry,
                       age_sec=age_sec, action_decided="grace_hold")
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
        # S63 P0: if TP25 already fired, only the remaining 50% of cost
        # is still allocated to this position. Booking -cost would double-charge.
        remaining_cost = cost * 0.5 if partial_done else cost
        _mark_closed(pid, note=f"{action}, zero wallet balance", realized_pnl=-remaining_cost)
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

    # S74 P3: log the poll that produced this action decision before we
    # enter the sell loop, so we have an exit_polls row even if the sell
    # loop crashes mid-flight.
    _log_exit_poll(pid, observed_price=price, pct_vs_entry=pct_vs_entry,
                   age_sec=age_sec, action_decided=action.lower())

    # S55 P3: escalate slippage on SLIPPAGE error, retry immediately (no cooldown).
    # Schedule: base (default 500=5%), 1500 (15%), 3000 (30%). Monotonically increasing.
    # If operator sets POSITION_SELL_SLIPPAGE_BPS above a tier, that tier is skipped.
    # Max cap at 3000bps — never escalate above 30% even if base > 3000.
    slippage_schedule = sorted({s for s in (slippage, 1500, 3000) if s >= slippage})

    # S80 P3b: quote sanity guard. Compute expected USDC at the observed price.
    # Allow up to SELL_QUOTE_MAX_GAP_PCT below observed before refusing.
    # On the SECOND blown quote, the floor is dropped to None so the position
    # still exits — better to take a bad price than hold while liquidity bleeds.
    quote_max_gap_pct = _cfg_float("SELL_QUOTE_MAX_GAP_PCT", "25.0")
    expected_usdc_at_observed = price * (qty_db * sell_fraction)
    quote_floor = expected_usdc_at_observed * (1 - quote_max_gap_pct / 100)
    quote_blown_count = 0

    success = False
    usdc_out = 0.0
    err_code: str | None = None
    for i, try_slip in enumerate(slippage_schedule):
        # S74 P3: per-attempt latency timer for instrumentation
        _attempt_t0 = time.time()
        # S80 P3b: pass the floor on first 2 attempts; drop guard on the last
        # attempt so the position can still exit.
        floor_arg = quote_floor if quote_blown_count < 2 else None
        if chain == "solana":
            success, usdc_out, err_code = _sell_solana(
                token_addr, sell_raw, symbol, try_slip,
                min_acceptable_usdc=floor_arg,
            )
        else:  # base
            success, usdc_out, err_code = _sell_base(token_addr, sell_raw, symbol, try_slip)
        _attempt_latency_ms = int((time.time() - _attempt_t0) * 1000)

        # S74 P3: log every attempt — successes, slippage failures, retries.
        _log_exit_attempt(
            pid,
            action=action,
            retry_tier=i,
            requested_slippage_bps=try_slip,
            observed_price=price,
            sl_price=sl,
            tp25_price=tp25,
            tp50_price=tp50,
            pct_vs_entry=pct_vs_entry,
            age_sec=age_sec,
            quantity_sold=qty_db * sell_fraction,
            executed_usdc=usdc_out,
            success=success,
            err_code=err_code,
            latency_ms=_attempt_latency_ms,
        )

        if success:
            if i > 0:
                log.info("Position #%d %s: %s succeeded on retry %d with slippage=%dbps",
                         pid, symbol, action, i, try_slip)
            break

        # S80 P3b: QUOTE_BLOWN — re-poll observed price and try the next tier.
        # The price may have recovered, in which case the next quote will be
        # within the floor. If not, the floor is recomputed at the new observed
        # price for the next iteration. Floor is dropped on attempt 3 so we
        # always exit before falling to cooldown.
        if err_code == "QUOTE_BLOWN":
            quote_blown_count += 1
            new_price = _fetch_price_usd(chain, token_addr)
            if new_price and new_price > 0:
                price = new_price
                expected_usdc_at_observed = price * (qty_db * sell_fraction)
                quote_floor = expected_usdc_at_observed * (1 - quote_max_gap_pct / 100)
                pct_vs_entry = ((price - entry) / entry) * 100
                log.info(
                    "Position #%d %s: re-polled price=$%.9f (%+.1f%%) — retrying with new floor=$%.4f",
                    pid, symbol, price, pct_vs_entry, quote_floor,
                )
            log.warning("Position #%d %s: %s quote blown — escalating to next tier",
                        pid, symbol, action)
            continue

        # Only retry on SLIPPAGE or TX_TOO_LARGE — other errors fall through to
        # cooldown. TX_TOO_LARGE means the current slippage tier produced a
        # multi-hop route that exceeds the ~1644-byte serialized tx limit.
        # A higher-slippage tier may yield a simpler route that fits. (S60 P2)
        if err_code not in ("SLIPPAGE", "TX_TOO_LARGE"):
            break
        log.warning("Position #%d %s: %s slippage=%dbps failed (%s) — escalating to next tier",
                    pid, symbol, action, try_slip, err_code)

    if not success:
        log.warning("Position #%d %s: %s sell did not confirm (err=%s, last_slippage=%dbps) — will retry after cooldown",
                    pid, symbol, action, err_code, try_slip)
        from scanner import _format_token_label
        _label_fail = _format_token_label(symbol, token_name)
        _send_telegram(
            f"⚠️ Position Manager: {action} trigger failed for {_label_fail} (#{pid})\n"
            f"price=${price:.9f} entry=${entry:.9f} ({pct_vs_entry:+.1f}%)\n"
            f"err={err_code} last_slippage={try_slip}bps\n"
            f"Will retry in {_ACTION_COOLDOWN_SECONDS}s. Inspect logs."
        )
        return

    # S63 P0: pnl math must reflect *remaining* cost basis, not full cost.
    # Old formula `usdc_out - (cost * sell_fraction)` was wrong on TP50-after-TP25
    # and SL-after-TP25 paths: when TP25 already sold 50% of the wallet, only
    # 50% of cost is still allocated to the remaining position, but the old
    # formula billed the whole `cost` against the TP50/SL proceeds, turning
    # +75% winners into booked losses (see #42 UNCTRUMP, #43 Untweeney).
    remaining_cost_fraction = 0.5 if partial_done else 1.0
    cost_attributed = cost * remaining_cost_fraction * sell_fraction
    realized_pnl = usdc_out - cost_attributed
    from scanner import _format_token_label as _fmt_exit
    _label_exit = _fmt_exit(symbol, token_name)
    _send_telegram(
        f"🔔 *Position Manager*: {action} exit — {_label_exit} (#{pid})\n"
        f"price=${price:.9f} entry=${entry:.9f} ({pct_vs_entry:+.1f}%)\n"
        f"sold {sell_fraction*100:.0f}% → ${usdc_out:.2f} USDC\n"
        f"realized vs pro-rata cost: ${realized_pnl:+.2f}"
    )

    if action == "TP25" and sell_fraction < 1.0:
        # S63 P1: persist the partial realization. Position stays open, but
        # realized_pnl_usd and daily_summary get the TP25 leg now so that
        # (a) the daily loss cap sees accurate state, (b) if the bot restarts
        # before TP50/SL, the remaining leg computes against correct cost,
        # and (c) realized pnl isn't lost if the position later closes via
        # the zero-wallet or dust path.
        _record_partial_realization(pid, realized_pnl)
        _partial_taken[pid] = True
        log.info(
            "Position #%d %s: TP25 partial complete (realized=$%+.2f), position remains open for TP50/SL",
            pid, symbol, realized_pnl,
        )
    else:
        # For TP50/SL after a TP25 partial, realized_pnl here is only the
        # second-leg gain/loss; the first leg was already booked on TP25.
        # _mark_closed adds to realized_pnl_usd incrementally (see below).
        _mark_closed(pid, note=action, realized_pnl=realized_pnl, exit_price=price)


def _load_open_positions() -> list[dict]:
    conn = _db_ro()
    try:
        cur = conn.execute(
            """SELECT id, chain, token_addr, token_name, symbol, entry_price,
                      quantity, cost_basis_usd, stop_loss_usd, take_profit_25,
                      take_profit_50, exchange, opened_at,
                      COALESCE(realized_pnl_usd, 0) AS realized_pnl_usd,
                      trail_high, trail_stop
               FROM positions WHERE status='open'"""
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


# S80 P3a: per-position next-check timestamp cache for adaptive polling.
# Keyed by position id -> unix ts when the next _evaluate_position call is due.
_next_check_ts: dict[int, float] = {}

# Per-position last-known pct_vs_entry. Avoids needing to re-fetch price
# just to decide whether to fetch price again on this tick.
_last_pct_cache: dict[int, float] = {}


def _due_for_check(
    pos_id: int,
    opened_at_str: str,
    now: float,
    slow_interval: int,
    fast_interval: int,
    fast_age_max: int,
    fast_drawdown_pct: float,
) -> bool:
    """
    S80 P3a: Decide if a position is due for a check this tick.

    Fast-poll path (fast_interval, default 15s): position age <
    fast_age_max seconds AND last observed pct_vs_entry <= -fast_drawdown_pct.

    Slow-poll path (slow_interval, default 60s): everything else.

    First time we see a position id, _next_check_ts has no entry, so we
    schedule it for now — guarantees at least one evaluation per session.
    """
    next_ts = _next_check_ts.get(pos_id)
    if next_ts is not None and now < next_ts:
        return False

    try:
        opened_dt = datetime.fromisoformat(opened_at_str.replace(" ", "T"))
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - opened_dt).total_seconds()
    except Exception:
        age_sec = 0.0

    last_pct = _last_pct_cache.get(pos_id, 0.0)
    is_fresh = age_sec < fast_age_max
    is_drawdown = last_pct <= -fast_drawdown_pct

    interval = fast_interval if (is_fresh and is_drawdown) else slow_interval
    _next_check_ts[pos_id] = now + interval
    return True


async def position_manager_loop() -> None:
    """Main coroutine — runs for the life of the bot process.

    S80 P3a: ticks every FAST_POLL_TICK_SECONDS (default 15s) and decides
    per-position whether enough time has elapsed since its last check.
    Young positions in drawdown get fast-polled (FAST_POLL_INTERVAL_SECONDS,
    default 15s); everything else stays on POSITION_CHECK_INTERVAL_SECONDS
    (default 60s).
    """
    if not _cfg_bool("POSITION_MANAGER_ENABLED", "true"):
        log.info("position_manager disabled via POSITION_MANAGER_ENABLED")
        return

    slow_interval     = _cfg_int("POSITION_CHECK_INTERVAL_SECONDS", "60")
    fast_interval     = _cfg_int("FAST_POLL_INTERVAL_SECONDS",      "15")
    fast_age_max      = _cfg_int("FAST_POLL_MAX_AGE_SECONDS",       "300")
    fast_drawdown_pct = _cfg_float("FAST_POLL_DRAWDOWN_PCT",        "3.0")
    tick              = _cfg_int("FAST_POLL_TICK_SECONDS",          "15")

    log.info(
        "position_manager: starting (tick=%ds, slow=%ds, fast=%ds when age<%ds and pct<=-%.1f%%)",
        tick, slow_interval, fast_interval, fast_age_max, fast_drawdown_pct,
    )

    while True:
        try:
            now = time.time()
            positions = _load_open_positions()
            if positions:
                live_ids = {p["id"] for p in positions}
                for stale in list(_next_check_ts.keys()):
                    if stale not in live_ids:
                        _next_check_ts.pop(stale, None)
                        _last_pct_cache.pop(stale, None)

                checked = 0
                for row in positions:
                    if _due_for_check(
                        row["id"], row["opened_at"], now,
                        slow_interval, fast_interval,
                        fast_age_max, fast_drawdown_pct,
                    ):
                        _evaluate_position(row)
                        checked += 1
                if checked:
                    log.debug("position_manager: %d/%d positions evaluated this tick",
                              checked, len(positions))
        except Exception as exc:
            log.error("position_manager loop error: %s", exc, exc_info=True)

        await asyncio.sleep(tick)


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
        success, usdc_out, _err = _sell_solana(token_addr, sell_raw, symbol, slippage)
    elif chain == 'base':
        sell_raw = _get_base_balance_raw(token_addr)
        if sell_raw <= 0:
            return {'success': False, 'position_id': int(position_id),
                    'symbol': symbol, 'chain': chain, 'usdc_received': 0.0,
                    'cost_basis': cost, 'realized_pnl': 0.0,
                    'error': 'zero token balance on-chain'}
        success, usdc_out, _err = _sell_base(token_addr, sell_raw, symbol, slippage)
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
    pos_qty = float(match.get('quantity') or 0)
    sn_exit_price = (float(usdc_out) / pos_qty) if pos_qty > 0 else None
    _mark_closed(int(position_id), note='SELL_NOW', realized_pnl=realized, exit_price=sn_exit_price)
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
