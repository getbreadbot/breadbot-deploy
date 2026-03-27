#!/usr/bin/env python3
"""
funding_arb_engine.py — Sprint 3B
Funding rate arbitrage engine.

Market-neutral strategy: simultaneously holds a long spot position and a short
perpetual futures position on the same asset. When perpetual funding rates are
positive (longs pay shorts), the short perp collects funding every 8 hours.
Spot and short cancel price exposure — strategy is delta-neutral.

Documented average annual return ~19% in 2025. Primary risk: funding rate
reversal (rate goes negative → engine pays instead of collects).

Exit rule: close pair trade if the 8h rate drops below FUNDING_RATE_EXIT_THRESHOLD
for three consecutive funding periods.

Exchange: Bybit (perp) + Binance.US (spot) for BTC and ETH.
SOL can be added once BTC/ETH pairs are stable.

New .env vars:
  FUNDING_ARB_ENABLED             true|false     (default false — opt-in)
  FUNDING_ARB_PAIRS               str            (default BTC,ETH)
  FUNDING_RATE_ENTRY_THRESHOLD    float          (default 0.01 — 0.01% per 8h ≈ 11% ann.)
  FUNDING_RATE_EXIT_THRESHOLD     float          (default 0.005)
  FUNDING_ARB_ALLOCATION_PCT      float          (default 0.20 — max % of portfolio)
  FUNDING_ARB_EXCHANGE            str            (default bybit)

New DB tables:
  funding_positions      — open/close timestamps, pair, entry, funding collected, pnl
  funding_rate_history   — 8h rate snapshots per pair (for /funding rates chart)
"""

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from config import DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TOTAL_PORTFOLIO_USD
import bybit_connector   as bybit
import binance_connector as binance

# Coinbase CFM — only loaded when FUNDING_ARB_EXCHANGE=coinbase_cfm
try:
    import coinbase_connector as coinbase_cfm
    _COINBASE_AVAILABLE = True
except ImportError:
    coinbase_cfm = None  # type: ignore
    _COINBASE_AVAILABLE = False

# Drift Protocol — only loaded when FUNDING_ARB_EXCHANGE=drift
try:
    import drift_connector as drift
    _DRIFT_AVAILABLE = True
except ImportError:
    drift = None  # type: ignore
    _DRIFT_AVAILABLE = False

log = logging.getLogger(__name__)
TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"

# ── Config ────────────────────────────────────────────────────────────────────
ARB_ENABLED        = os.getenv("FUNDING_ARB_ENABLED",          "false").lower() == "true"
ARB_PAIRS          = [p.strip().upper() for p in os.getenv("FUNDING_ARB_PAIRS", "BTC,ETH").split(",")]
ENTRY_THRESHOLD    = float(os.getenv("FUNDING_RATE_ENTRY_THRESHOLD", "0.01"))   # % per 8h
EXIT_THRESHOLD     = float(os.getenv("FUNDING_RATE_EXIT_THRESHOLD",  "0.005"))  # % per 8h
ALLOCATION_PCT     = float(os.getenv("FUNDING_ARB_ALLOCATION_PCT",   "0.20"))
ARB_EXCHANGE       = os.getenv("FUNDING_ARB_EXCHANGE", "bybit").lower()

CONSECUTIVE_EXIT   = 3       # close after this many consecutive periods below exit threshold
POLL_INTERVAL      = 3600    # check fills + rates every hour (funding is every 8h)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class FundingPosition:
    db_id:              int
    pair:               str          # e.g. "BTC"
    spot_symbol:        str          # e.g. "BTCUSDT" for Binance spot
    perp_symbol:        str          # e.g. "BTCUSDT" for Bybit linear perp
    entry_price:        float
    quantity:           float        # in base asset (BTC, ETH)
    spot_order_id:      Optional[str] = None
    perp_order_id:      Optional[str] = None
    funding_collected:  float        = 0.0
    below_exit_count:   int          = 0   # consecutive periods below EXIT_THRESHOLD
    open:               bool         = True


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_funding_tables() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS funding_positions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                pair                TEXT    NOT NULL,
                spot_symbol         TEXT    NOT NULL,
                perp_symbol         TEXT    NOT NULL,
                entry_price         REAL    NOT NULL,
                quantity            REAL    NOT NULL,
                funding_collected   REAL    DEFAULT 0,
                realized_pnl        REAL    DEFAULT 0,
                status              TEXT    DEFAULT 'open',
                opened_at           TEXT    DEFAULT (datetime('now')),
                closed_at           TEXT,
                close_reason        TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS funding_rate_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pair        TEXT    NOT NULL,
                rate        REAL    NOT NULL,
                annualized  REAL    NOT NULL,
                recorded_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def db_open_position(pair: str, spot_sym: str, perp_sym: str,
                      entry_price: float, quantity: float) -> int:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("""
            INSERT INTO funding_positions
              (pair, spot_symbol, perp_symbol, entry_price, quantity)
            VALUES (?, ?, ?, ?, ?)
        """, (pair, spot_sym, perp_sym, entry_price, quantity))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def db_update_funding(pos_id: int, funding_collected: float) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            UPDATE funding_positions
            SET funding_collected = funding_collected + ?
            WHERE id = ?
        """, (funding_collected, pos_id))
        conn.commit()
    finally:
        conn.close()


def db_close_position(pos_id: int, realized_pnl: float, reason: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            UPDATE funding_positions
            SET status='closed', realized_pnl=?, closed_at=datetime('now'),
                close_reason=?
            WHERE id=?
        """, (realized_pnl, reason, pos_id))
        conn.commit()
    finally:
        conn.close()


def db_log_rate(pair: str, rate: float) -> None:
    """Log an 8h funding rate snapshot."""
    ann = round(rate * 3 * 365 * 100, 4)   # annualised %
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            INSERT INTO funding_rate_history (pair, rate, annualized)
            VALUES (?, ?, ?)
        """, (pair, rate, ann))
        conn.commit()
    finally:
        conn.close()


def db_get_open_positions() -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = conn.execute("""
            SELECT id, pair, entry_price, quantity, funding_collected,
                   realized_pnl, opened_at
            FROM funding_positions WHERE status='open'
            ORDER BY opened_at DESC
        """).fetchall()
        conn.close()
        return [{"id": r[0], "pair": r[1], "entry_price": r[2],
                 "quantity": r[3], "funding_collected": r[4],
                 "realized_pnl": r[5], "opened_at": r[6]} for r in rows]
    except Exception as exc:
        log.error("db_get_open_positions: %s", exc)
        return []


def db_get_rate_history(pair: str, limit: int = 24) -> list[dict]:
    """Return last N rate readings for a pair (for /funding rates command)."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = conn.execute("""
            SELECT rate, annualized, recorded_at
            FROM funding_rate_history
            WHERE pair=? ORDER BY id DESC LIMIT ?
        """, (pair.upper(), limit)).fetchall()
        conn.close()
        return [{"rate": r[0], "annualized": r[1], "recorded_at": r[2]} for r in rows]
    except Exception as exc:
        log.error("db_get_rate_history: %s", exc)
        return []


# ── Funding rate fetcher ──────────────────────────────────────────────────────

def get_funding_rates(pairs: list[str]) -> dict[str, float]:
    """
    Fetch current 8h (or equivalent) funding rates for all configured pairs.
    Routes to the correct exchange based on FUNDING_ARB_EXCHANGE env var.

    Supported exchanges:
      bybit         — Bybit V5 perpetuals (default)
      coinbase_cfm  — Coinbase Financial Markets perpetuals (US-legal)
      binance       — Binance futures (for reference only, US-blocked)

    Returns {pair: rate} where rate is the 8h-equivalent figure.
    Falls back to ENTRY_THRESHOLD on API failure.
    """
    rates: dict[str, float] = {}
    for pair in pairs:
        try:
            if ARB_EXCHANGE == "coinbase_cfm":
                if not _COINBASE_AVAILABLE:
                    log.error("coinbase_connector not available — cannot fetch CFM rates")
                    rates[pair] = ENTRY_THRESHOLD
                    continue
                result = coinbase_cfm.get_funding_rate(pair)
                rate   = float(result.get("fundingRate", 0))   # already converted to 8h
            elif ARB_EXCHANGE == "drift":
                if not _DRIFT_AVAILABLE:
                    log.error("drift_connector not available — cannot fetch Drift rates")
                    rates[pair] = ENTRY_THRESHOLD
                    continue
                result = drift.get_funding_rate(pair)  # sync wrapper
                rate   = float(result.get("fundingRate", 0))   # 8h equivalent
            else:
                # Default: Bybit
                symbol  = f"{pair}USDT"
                history = bybit.get_funding_rate(symbol, limit=1)
                rate    = float(history[0].get("fundingRate", 0)) if history else ENTRY_THRESHOLD

            rates[pair] = rate
            ann = rate * 3 * 365 * 100
            log.info("Funding rate %s [%s]: %.6f (%.2f%% ann.)",
                     pair, ARB_EXCHANGE, rate, ann)
        except Exception as exc:
            log.warning("Funding rate fetch failed for %s [%s]: %s — using fallback",
                        pair, ARB_EXCHANGE, exc)
            rates[pair] = ENTRY_THRESHOLD
    return rates


def evaluate_entry(pair: str, rate: float, open_positions: list[dict]) -> bool:
    """
    Return True if rate exceeds ENTRY_THRESHOLD and we don't already
    have an open position on this pair.
    """
    already_open = any(p["pair"] == pair for p in open_positions)
    if already_open:
        log.debug("evaluate_entry %s: already have open position", pair)
        return False
    if rate < ENTRY_THRESHOLD / 100:
        log.debug("evaluate_entry %s: rate %.6f below threshold %.6f",
                  pair, rate, ENTRY_THRESHOLD / 100)
        return False
    return True


def should_close(pos: FundingPosition, rate: float) -> tuple[bool, str]:
    """
    Return (True, reason) if the position should be closed.
    Closes after CONSECUTIVE_EXIT periods below EXIT_THRESHOLD,
    or immediately if rate is negative.
    """
    rate_pct = rate * 100   # convert to % for threshold comparison
    if rate < 0:
        return True, f"Rate gone negative ({rate_pct:.4f}%) — closing immediately"
    if rate_pct < EXIT_THRESHOLD:
        pos.below_exit_count += 1
        if pos.below_exit_count >= CONSECUTIVE_EXIT:
            return True, (
                f"Rate {rate_pct:.4f}% below exit threshold {EXIT_THRESHOLD}% "
                f"for {CONSECUTIVE_EXIT} consecutive periods"
            )
    else:
        pos.below_exit_count = 0   # reset counter if rate recovers
    return False, ""


# ── Position sizing ───────────────────────────────────────────────────────────

def calculate_position_size(pair: str, price: float) -> float:
    """
    Max allocation split equally across active pairs.
    Returns quantity in base asset (BTC, ETH), rounded to exchange minimums.
    """
    max_usd    = TOTAL_PORTFOLIO_USD * ALLOCATION_PCT / max(len(ARB_PAIRS), 1)
    # Each side (spot + perp) uses half the allocation
    side_usd   = max_usd / 2
    quantity   = side_usd / price
    # Round to 3dp for BTC, 2dp for ETH/SOL
    decimals   = 5 if price > 10_000 else 3
    return round(quantity, decimals)


# ── Trade execution ───────────────────────────────────────────────────────────

def open_pair_trade(pair: str) -> Optional[FundingPosition]:
    """
    Place simultaneous spot BUY (Binance.US) and perp SELL (Bybit).
    Returns a FundingPosition on success, None on failure.

    Both legs use Market orders so they execute immediately at the same price.
    If either leg fails, logs a critical error — caller must monitor for
    unhedged positions and close the successful leg manually.
    """
    spot_symbol = f"{pair}USDT"
    perp_symbol = f"{pair}USDT"

    # Get current mark price from Bybit for sizing
    try:
        ticker      = bybit.get_ticker(perp_symbol)
        entry_price = float(ticker.get("markPrice") or ticker.get("lastPrice", 0))
    except Exception as exc:
        log.error("open_pair_trade: failed to fetch %s price: %s", pair, exc)
        return None

    if entry_price <= 0:
        log.error("open_pair_trade: invalid price %.2f for %s", entry_price, pair)
        return None

    quantity = calculate_position_size(pair, entry_price)
    if quantity <= 0:
        log.error("open_pair_trade: calculated quantity %.6f too small", quantity)
        return None

    log.info("Opening arb pair: %s | price=%.2f qty=%.6f", pair, entry_price, quantity)

    spot_order_id = None
    perp_order_id = None

    if ARB_EXCHANGE == "coinbase_cfm":
        return _open_pair_cfm(pair, entry_price, quantity)

    if ARB_EXCHANGE == "drift":
        return _open_pair_drift(pair, entry_price, quantity)

    # ── Bybit / Binance.US path ────────────────────────────────────────────────
    # Leg 1: spot BUY on Binance.US
    try:
        spot_result   = binance.place_order(spot_symbol, "BUY", "MARKET", quantity)
        spot_order_id = str(spot_result.get("orderId", ""))
        log.info("Spot BUY placed: %s orderId=%s", spot_symbol, spot_order_id)
    except Exception as exc:
        log.critical("SPOT LEG FAILED for %s: %s — no perp placed, position is flat", pair, exc)
        return None

    # Leg 2: perp SHORT on Bybit — set leverage to 1x (cash-neutral)
    try:
        bybit.set_leverage(perp_symbol, 1)
        perp_result   = bybit.place_perp_order(perp_symbol, "Sell", quantity, order_type="Market")
        perp_order_id = str(perp_result.get("orderId", ""))
        log.info("Perp SELL placed: %s orderId=%s", perp_symbol, perp_order_id)
    except Exception as exc:
        log.critical(
            "PERP LEG FAILED for %s: %s — spot is LONG, perp NOT opened. "
            "MANUAL ACTION REQUIRED: close spot position %s qty=%.6f",
            pair, exc, spot_symbol, quantity,
        )
        return None

    # Log to DB
    pos_id   = db_open_position(pair, spot_symbol, perp_symbol, entry_price, quantity)
    position = FundingPosition(
        db_id          = pos_id,
        pair           = pair,
        spot_symbol    = spot_symbol,
        perp_symbol    = perp_symbol,
        entry_price    = entry_price,
        quantity       = quantity,
        spot_order_id  = spot_order_id,
        perp_order_id  = perp_order_id,
    )
    log.info("Arb pair opened: pos_id=%d %s entry=%.2f qty=%.6f",
             pos_id, pair, entry_price, quantity)
    return position


def _open_pair_drift(pair: str, entry_price: float, quantity: float) -> Optional[FundingPosition]:
    """
    Open a funding arb pair using Drift Protocol for both legs.

    Spot leg:  BUY on Jupiter V6 via solana_executor (USDC → base token)
    Perp leg:  SHORT on Drift Protocol (delta-neutral, unified cross-margin)

    The spot token is held in the wallet. The perp short hedges price exposure.
    Both use the existing Solana wallet keypair — no new accounts needed.
    Requires USDC in the wallet for spot buy AND deposited in Drift for perp margin.
    """
    if not _DRIFT_AVAILABLE:
        log.error("_open_pair_drift: drift_connector not available")
        return None

    spot_symbol = f"{pair}USDT"   # logical identifier
    perp_symbol = f"{pair}-PERP"
    size_usd    = quantity * entry_price  # USD notional for each leg

    spot_order_id = None
    perp_order_id = None

    # Leg 1: spot BUY via Jupiter V6 on Solana
    try:
        import solana_executor
        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        SOL_MINTS = {
            "SOL": "So11111111111111111111111111111111111111112",
            "BTC": "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",  # BTC (wBTC)
            "ETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (wETH)
        }
        token_mint = SOL_MINTS.get(pair.upper())
        if not token_mint:
            log.error("_open_pair_drift: no SPL mint for %s", pair)
            return None
        usdc_lamports = int(size_usd * 1_000_000)
        quote    = solana_executor.get_quote(USDC_MINT, token_mint, usdc_lamports)
        tx_b64   = solana_executor.build_swap_tx(quote)
        sig      = solana_executor.sign_and_send(tx_b64)
        confirmed = solana_executor.confirm_tx(sig)
        spot_order_id = sig
        if not confirmed:
            log.critical("_open_pair_drift: spot tx not confirmed for %s — no perp opened", pair)
            return None
        log.info("Drift spot BUY (Jupiter): %s $%.2f sig=%s", pair, size_usd, sig)
    except Exception as exc:
        log.critical("_open_pair_drift spot leg FAILED for %s: %s", pair, exc)
        return None

    # Leg 2: perp SHORT on Drift (sync wrapper)
    try:
        result = drift.open_short_perp_sync(pair, size_usd)
        if not result.get("success"):
            raise RuntimeError(result.get("error", "unknown error"))
        perp_order_id = result.get("tx_sig", "")
        log.info("Drift perp SHORT: %s $%.2f sig=%s", pair, size_usd, perp_order_id)
    except Exception as exc:
        log.critical(
            "_open_pair_drift perp leg FAILED for %s: %s — spot LONG not hedged. "
            "MANUAL ACTION REQUIRED: sell %s spot position", pair, exc, pair
        )
        return None

    pos_id   = db_open_position(pair, spot_symbol, perp_symbol, entry_price, quantity)
    position = FundingPosition(
        db_id         = pos_id,
        pair          = pair,
        spot_symbol   = spot_symbol,
        perp_symbol   = perp_symbol,
        entry_price   = entry_price,
        quantity      = quantity,
        spot_order_id = spot_order_id,
        perp_order_id = perp_order_id,
    )
    log.info("Drift arb pair opened: pos_id=%d %s entry=%.2f qty=%.6f",
             pos_id, pair, entry_price, quantity)
    return position


def _open_pair_cfm(pair: str, entry_price: float, quantity: float) -> Optional[FundingPosition]:
    """
    Open a funding arb pair using Coinbase CFM for both legs.
    Both spot and perp are on Coinbase — no cross-exchange capital split.

    Spot leg:  BUY on Coinbase Advanced Trade (BTC-USDC / ETH-USDC)
    Perp leg:  SHORT on Coinbase CFM (BTC-PERP-INTX / ETH-PERP-INTX)
    """
    if not _COINBASE_AVAILABLE:
        log.error("_open_pair_cfm: coinbase_connector not available")
        return None

    spot_symbol = f"{pair}USDT"   # used as logical identifier; CB uses BTC-USDC
    perp_symbol = f"{pair}-PERP-INTX"
    base_size   = str(round(quantity, 8))

    spot_order_id = None
    perp_order_id = None

    # Leg 1: spot BUY on Coinbase Advanced Trade
    try:
        product_id  = f"{pair}-USDC"
        spot_result = coinbase_cfm.place_spot_order(product_id, "BUY", base_size)
        spot_order_id = str(spot_result.get("order_id", ""))
        log.info("CB spot BUY placed: %s orderId=%s", product_id, spot_order_id)
    except Exception as exc:
        log.critical("CFM spot leg FAILED for %s: %s — no perp placed", pair, exc)
        return None

    # Leg 2: perp SHORT on Coinbase CFM (1x leverage, delta-neutral)
    try:
        perp_result   = coinbase_cfm.place_perp_order(pair, "SELL", quantity, leverage=1)
        perp_order_id = str(perp_result.get("order_id", ""))
        log.info("CFM perp SELL placed: %s orderId=%s", perp_symbol, perp_order_id)
    except Exception as exc:
        log.critical(
            "CFM perp leg FAILED for %s: %s — spot LONG not hedged. "
            "MANUAL ACTION REQUIRED: close CB spot position %s qty=%s",
            pair, exc, f"{pair}-USDC", base_size,
        )
        return None

    pos_id   = db_open_position(pair, spot_symbol, perp_symbol, entry_price, quantity)
    position = FundingPosition(
        db_id         = pos_id,
        pair          = pair,
        spot_symbol   = spot_symbol,
        perp_symbol   = perp_symbol,
        entry_price   = entry_price,
        quantity      = quantity,
        spot_order_id = spot_order_id,
        perp_order_id = perp_order_id,
    )
    log.info("CFM arb pair opened: pos_id=%d %s entry=%.2f qty=%.6f",
             pos_id, pair, entry_price, quantity)
    return position


def close_pair_trade(pos: FundingPosition, reason: str) -> float:
    """
    Close both legs of a funding arb pair.
    Spot SELL on Binance.US + perp BUY (reduce_only) on Bybit.
    Returns estimated realized PnL (funding collected minus any spread cost).
    """
    log.info("Closing arb pair: %s pos_id=%d reason=%s", pos.pair, pos.db_id, reason)

    if ARB_EXCHANGE == "drift" and _DRIFT_AVAILABLE:
        # ── Drift Protocol close path ──────────────────────────────────────────
        # Spot SELL via Jupiter V6
        try:
            import solana_executor
            SOL_MINTS = {
                "SOL": "So11111111111111111111111111111111111111112",
                "BTC": "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",
                "ETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
            }
            USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            token_mint = SOL_MINTS.get(pos.pair.upper())
            if token_mint:
                token_lamports = int(pos.quantity * 1_000_000_000)
                quote    = solana_executor.get_quote(token_mint, USDC_MINT, token_lamports)
                tx_b64   = solana_executor.build_swap_tx(quote)
                sig      = solana_executor.sign_and_send(tx_b64)
                solana_executor.confirm_tx(sig)
                log.info("Drift spot SELL (Jupiter): %s sig=%s", pos.pair, sig)
        except Exception as exc:
            log.error("Drift spot close failed for %s: %s", pos.pair, exc)
        # Perp close on Drift
        try:
            from drift_connector import MARKET_INDEX
            midx   = MARKET_INDEX.get(pos.pair.upper(), -1)
            result = drift.close_perp_position_sync(midx)
            log.info("Drift perp close: %s result=%s", pos.pair, result)
        except Exception as exc:
            log.error("Drift perp close failed for %s: %s", pos.pair, exc)

    elif ARB_EXCHANGE == "coinbase_cfm" and _COINBASE_AVAILABLE:
        # ── Coinbase CFM close path ────────────────────────────────────────────
        # Spot SELL on Coinbase Advanced Trade
        try:
            product_id = f"{pos.pair}-USDC"
            coinbase_cfm.place_spot_order(product_id, "SELL", str(round(pos.quantity, 8)))
            log.info("CB spot SELL executed: %s qty=%.6f", product_id, pos.quantity)
        except Exception as exc:
            log.error("CB spot close failed for %s: %s", pos.pair, exc)
        # Perp BUY (reduce-only) on Coinbase CFM
        try:
            coinbase_cfm.close_perp_position(pos.pair)
            log.info("CFM perp close executed: %s", pos.pair)
        except Exception as exc:
            log.error("CFM perp close failed for %s: %s", pos.pair, exc)
    else:
        # ── Default: Bybit / Binance.US close path ─────────────────────────────
        # Spot SELL
        try:
            binance.place_order(pos.spot_symbol, "SELL", "MARKET", pos.quantity)
            log.info("Spot SELL executed: %s qty=%.6f", pos.spot_symbol, pos.quantity)
        except Exception as exc:
            log.error("Spot close failed for %s: %s", pos.pair, exc)

        # Perp BUY (close short)
        try:
            bybit.place_perp_order(pos.perp_symbol, "Buy", pos.quantity,
                                   order_type="Market", reduce_only=True)
            log.info("Perp BUY (close) executed: %s qty=%.6f", pos.perp_symbol, pos.quantity)
        except Exception as exc:
            log.error("Perp close failed for %s: %s", pos.pair, exc)

    # PnL estimate: funding collected is the primary return
    # Spread cost is typically 0.02-0.06% round-trip on liquid pairs
    estimated_spread_cost = pos.entry_price * pos.quantity * 0.0004  # 0.04% conservative
    realized = round(pos.funding_collected - estimated_spread_cost, 6)

    db_close_position(pos.db_id, realized, reason)
    pos.open = False
    log.info("Arb pair closed: %s pnl=%.4f funding=%.4f",
             pos.pair, realized, pos.funding_collected)
    return realized


# ── Telegram ──────────────────────────────────────────────────────────────────

async def _tg_send(client: httpx.AsyncClient, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = TELEGRAM_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    try:
        await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
        }, timeout=10)
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


async def handle_funding_command(client: httpx.AsyncClient,
                                  subcommand: str,
                                  engine: "FundingArbEngine") -> None:
    """
    Handle /funding Telegram commands.
      /funding              → current rates for all monitored pairs
      /funding positions    → open arb pairs with cumulative income
      /funding rates [pair] → rate history for a specific pair
    """
    parts = subcommand.strip().split()
    sub   = parts[0].lower() if parts else ""

    if sub == "positions":
        rows = db_get_open_positions()
        if not rows:
            await _tg_send(client, "No open funding arb positions.")
            return
        total_funding = sum(r["funding_collected"] for r in rows)
        lines = [f"Funding Arb Positions ({len(rows)})\n"]
        for r in rows:
            ann_rate = ENTRY_THRESHOLD   # approximate
            lines.append(
                f"{r['pair']}/USDT\n"
                f"  Entry:     ${r['entry_price']:,.2f}\n"
                f"  Qty:       {r['quantity']:.6f}\n"
                f"  Funding:   ${r['funding_collected']:.4f} collected\n"
                f"  Opened:    {r['opened_at'][:10]}"
            )
        lines.append(f"\nTotal funding collected: ${total_funding:.4f}")
        await _tg_send(client, "\n".join(lines))
        return

    if sub == "rates" and len(parts) >= 2:
        pair    = parts[1].upper()
        history = db_get_rate_history(pair, 24)
        if not history:
            await _tg_send(client, f"No rate history for {pair} yet.")
            return
        lines = [f"Funding Rate History — {pair} (last {len(history)} readings)\n"]
        for r in history:
            direction = "▲" if r["rate"] >= 0 else "▼"
            lines.append(
                f"{r['recorded_at'][:16]}  "
                f"{direction} {r['rate']*100:.4f}%  "
                f"({r['annualized']:.2f}% ann.)"
            )
        await _tg_send(client, "\n".join(lines))
        return

    # Default: show current rates
    rates = get_funding_rates(ARB_PAIRS)
    lines = [f"Funding Rates — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"]
    for pair, rate in rates.items():
        ann       = rate * 3 * 365 * 100
        status    = "✅ ABOVE ENTRY" if rate * 100 >= ENTRY_THRESHOLD else "— below threshold"
        lines.append(f"{pair}: {rate*100:.4f}%/8h  ({ann:.2f}%/yr)  {status}")
    lines.append(f"\nEntry threshold: {ENTRY_THRESHOLD}%/8h ({ENTRY_THRESHOLD*3*365:.1f}%/yr)")
    lines.append(f"Exit threshold:  {EXIT_THRESHOLD}%/8h")
    await _tg_send(client, "\n".join(lines))


# ── Engine class ──────────────────────────────────────────────────────────────

class FundingArbEngine:
    """Manages the full funding arb lifecycle."""

    def __init__(self) -> None:
        self._positions: list[FundingPosition] = []
        ensure_funding_tables()
        log.info("FundingArbEngine initialised (pairs=%s)", ARB_PAIRS)

    @property
    def open_positions(self) -> list[FundingPosition]:
        return [p for p in self._positions if p.open]

    async def evaluate_and_act(self, client: httpx.AsyncClient) -> None:
        """
        Main tick: fetch rates, log them, open new pairs where warranted,
        collect estimated funding, check exit conditions on open positions.
        Called every POLL_INTERVAL seconds.
        """
        if not ARB_ENABLED:
            return

        rates     = get_funding_rates(ARB_PAIRS)
        open_rows = db_get_open_positions()

        for pair, rate in rates.items():
            # Always log the rate
            db_log_rate(pair, rate)

            rate_pct = rate * 100
            ann      = rate * 3 * 365 * 100

            # Open new position if threshold exceeded
            if evaluate_entry(pair, rate_pct, open_rows):
                log.info(
                    "Entry condition met for %s: rate=%.4f%% (%.2f%% ann.)",
                    pair, rate_pct, ann,
                )
                pos = open_pair_trade(pair)
                if pos:
                    self._positions.append(pos)
                    open_rows = db_get_open_positions()   # refresh
                    await _tg_send(
                        client,
                        f"Funding Arb OPENED\n\n"
                        f"Pair:     {pair}/USDT\n"
                        f"Entry:    ${pos.entry_price:,.2f}\n"
                        f"Qty:      {pos.quantity:.6f}\n"
                        f"Rate:     {rate_pct:.4f}%/8h ({ann:.2f}%/yr)\n"
                        f"Strategy: long spot + short perp (delta neutral)",
                    )

        # Check exit conditions on open in-memory positions
        for pos in list(self.open_positions):
            rate     = rates.get(pos.pair, 0)
            # Estimate funding payment since last check
            # Funding is paid every 8h; we check hourly so add 1/8 of the payment
            funding_payment = rate * pos.quantity * pos.entry_price / 8
            if funding_payment > 0:
                pos.funding_collected = round(pos.funding_collected + funding_payment, 6)
                db_update_funding(pos.db_id, funding_payment)

            close, reason = should_close(pos, rate)
            if close:
                pnl = close_pair_trade(pos, reason)
                await _tg_send(
                    client,
                    f"Funding Arb CLOSED\n\n"
                    f"Pair:     {pos.pair}/USDT\n"
                    f"Reason:   {reason}\n"
                    f"Funding collected: ${pos.funding_collected:.4f}\n"
                    f"Est. net PnL:      ${pnl:.4f}",
                )


# ── Main loop ─────────────────────────────────────────────────────────────────

async def funding_arb_loop(engine: FundingArbEngine) -> None:
    """Runs alongside scanner. Evaluates rates and acts every POLL_INTERVAL seconds."""
    if not ARB_ENABLED:
        log.info("Funding arb engine disabled (FUNDING_ARB_ENABLED=false)")
        return

    log.info(
        "Funding arb loop started | pairs=%s entry=%.4f%% exit=%.4f%% alloc=%.0f%%",
        ARB_PAIRS, ENTRY_THRESHOLD, EXIT_THRESHOLD, ALLOCATION_PCT * 100,
    )
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await engine.evaluate_and_act(client)
            except Exception as exc:
                log.error("Funding arb loop error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ensure_funding_tables()
    print(
        f"Funding arb self-test\n"
        f"  Pairs:           {ARB_PAIRS}\n"
        f"  Entry threshold: {ENTRY_THRESHOLD}%/8h  ({ENTRY_THRESHOLD*3*365:.1f}%/yr)\n"
        f"  Exit threshold:  {EXIT_THRESHOLD}%/8h\n"
        f"  Allocation:      {ALLOCATION_PCT*100:.0f}% of portfolio\n"
        f"  Portfolio USD:   ${TOTAL_PORTFOLIO_USD:,.0f}\n"
    )

    # Rate fetch (requires Bybit API keys in .env)
    print("Fetching current funding rates...")
    try:
        rates = get_funding_rates(ARB_PAIRS)
        for pair, rate in rates.items():
            ann     = rate * 3 * 365 * 100
            above   = "ABOVE ENTRY THRESHOLD" if rate * 100 >= ENTRY_THRESHOLD else "below threshold"
            print(f"  {pair}: {rate*100:.4f}%/8h  ({ann:.2f}%/yr)  — {above}")

        # Position sizing preview
        print("\nPosition sizing preview (no orders placed):")
        for pair in ARB_PAIRS:
            rate  = rates.get(pair, 0)
            price = 70000.0 if pair == "BTC" else 3500.0   # approximate
            try:
                ticker = bybit.get_ticker(f"{pair}USDT")
                price  = float(ticker.get("markPrice") or ticker.get("lastPrice", price))
            except Exception:
                pass
            qty       = calculate_position_size(pair, price)
            usd_value = qty * price
            print(f"  {pair}: ${price:,.2f} → qty={qty:.6f} (~${usd_value:,.2f} per leg)")

    except Exception as exc:
        print(f"Rate fetch failed (API keys configured?): {exc}")

    # DB table check
    print(f"\nDB tables: funding_positions, funding_rate_history — created OK")
    print(f"Open positions in DB: {len(db_get_open_positions())}")
