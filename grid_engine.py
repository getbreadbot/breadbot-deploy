#!/usr/bin/env python3
"""
grid_engine.py — Sprint 3A
Grid trading engine with trend guard.

Places buy and sell limit orders at preset price intervals within a defined range.
Every completed buy-sell cycle captures the grid spacing as profit. Operates on
BTC/USDT and ETH/USDT on Binance.US by default.

States: STANDBY → ACTIVE → PAUSED (then back to STANDBY or ACTIVE)

Trend guard: checks 4-hour RSI before activating. If RSI > 65 or < 35,
strong directional momentum is present and grid trading is dangerous — engine
stays in STANDBY until conditions normalize.

New .env vars:
  GRID_ENABLED          true|false      (default false — opt-in)
  GRID_PAIR             str             (default BTC/USDT)
  GRID_UPPER_PCT        float           (default 10 — upper boundary % above entry)
  GRID_LOWER_PCT        float           (default 10 — lower boundary % below entry)
  GRID_NUM_LEVELS       int             (default 20 — number of grid lines)
  GRID_ALLOCATION_USD   float           (default 500 — capital to deploy)
  GRID_EXCHANGE         str             (default binance)
  GRID_RSI_GUARD        true|false      (default true — pause if RSI exits 35-65)

New DB tables:
  grid_sessions  — start/stop timestamps, pair, total profit
  grid_fills     — every completed buy-sell cycle with entry, exit, net profit
"""

import asyncio
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from config import DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
import binance_connector as binance

log = logging.getLogger(__name__)

TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"

# ── Config ────────────────────────────────────────────────────────────────────
GRID_ENABLED       = os.getenv("GRID_ENABLED",        "false").lower() == "true"


def is_enabled() -> bool:
    """Runtime-fresh read of the grid_enabled flag.
    Priority: bot_config DB > GRID_ENABLED env > False.
    Safe to call inside the engine loop — the panel can toggle this
    from the Strategies page without a bot restart."""
    try:
        from config import _bool_config
        return _bool_config("grid_enabled", "GRID_ENABLED", default=False)
    except Exception:
        # Fallback to module constant if config import ever fails
        return GRID_ENABLED


def _set_enabled(value: bool) -> None:
    """Write grid_enabled to bot_config. Called by engine.start/stop so that
    any activation path (Telegram, panel, MCP) keeps the DB flag in sync with
    the live engine state. Logs but does not raise on write failure — DB sync
    is best-effort, engine state is authoritative."""
    try:
        from config import DB_PATH
        from datetime import datetime, timezone
        import sqlite3
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("grid_enabled", "true" if value else "false", now),
            )
            conn.commit()
        finally:
            conn.close()
        log.info("grid_enabled flag written: %s", value)
    except Exception as exc:
        log.warning("Failed to write grid_enabled flag (%s): %s", value, exc)
GRID_PAIR          = os.getenv("GRID_PAIR",            "BTC/USDT").upper().replace("/", "")
GRID_UPPER_PCT     = float(os.getenv("GRID_UPPER_PCT",       "10"))
GRID_LOWER_PCT     = float(os.getenv("GRID_LOWER_PCT",       "10"))
GRID_NUM_LEVELS    = int(os.getenv("GRID_NUM_LEVELS",        "20"))
GRID_ALLOCATION    = float(os.getenv("GRID_ALLOCATION_USD",  "500"))
GRID_EXCHANGE      = os.getenv("GRID_EXCHANGE",        "binance").lower()
GRID_RSI_GUARD     = os.getenv("GRID_RSI_GUARD",       "true").lower() == "true"

RSI_UPPER_BLOCK    = 65.0
RSI_LOWER_BLOCK    = 35.0
POLL_INTERVAL      = 60    # seconds between fill-check cycles
RSI_CANDLE_LIMIT   = 50    # candles fetched for RSI calculation (need 14+)


# __ Exchange filter quantization (S68 P1) ___________________________________
# Binance.US rejects orders whose qty is not an integer multiple of LOT_SIZE
# stepSize, or whose price is not a tickSize multiple. Error -1013 "Filter
# failure: LOT_SIZE". Cache filters per symbol on first use.

_FILTER_CACHE: dict[str, dict] = {}


def _get_symbol_filters(symbol: str) -> dict:
    """Return dict with keys: step_size, min_qty, tick_size, min_notional.
    Cached per symbol. Returns empty dict if exchange info unavailable."""
    sym = symbol.upper()
    if sym in _FILTER_CACHE:
        return _FILTER_CACHE[sym]
    out: dict = {}
    try:
        info = binance.get_exchange_info(sym)
        symbols = info.get("symbols", []) if isinstance(info, dict) else []
        if not symbols:
            log.warning("Exchange info returned no symbol %s", sym)
            _FILTER_CACHE[sym] = out
            return out
        for f in symbols[0].get("filters", []):
            ftype = f.get("filterType")
            if ftype == "LOT_SIZE":
                out["step_size"] = float(f.get("stepSize", 0) or 0)
                out["min_qty"]   = float(f.get("minQty", 0) or 0)
            elif ftype == "PRICE_FILTER":
                out["tick_size"] = float(f.get("tickSize", 0) or 0)
            elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                mn = f.get("minNotional") or f.get("notional") or 0
                out["min_notional"] = float(mn or 0)
    except Exception as exc:
        log.warning("Failed to fetch exchange filters for %s: %s", sym, exc)
    _FILTER_CACHE[sym] = out
    return out


def _floor_to_step(value: float, step: float) -> float:
    """Floor value to the nearest multiple of step."""
    if step <= 0:
        return value
    return math.floor(round(value / step, 10)) * step


def _quantize_order(symbol: str, quantity: float, price):
    """Quantize qty to stepSize (floor) and price to tickSize (floor).

    Returns (qty, price) tuple on success, or None if qty falls below minQty
    after quantization (caller should skip that level).

    price=None is passed through unchanged (for MARKET orders)."""
    f = _get_symbol_filters(symbol)
    step = f.get("step_size", 0)
    min_qty = f.get("min_qty", 0)
    tick = f.get("tick_size", 0)

    qty = _floor_to_step(quantity, step) if step > 0 else quantity
    if min_qty > 0 and qty < min_qty:
        log.warning(
            "Quantized qty %.10f below minQty %.10f for %s (raw=%.10f step=%s); "
            "skipping order",
            qty, min_qty, symbol, quantity, step,
        )
        return None
    if step > 0:
        decimals = max(0, -int(round(math.log10(step)))) if step < 1 else 0
        qty = round(qty, decimals)

    out_price = price
    if price is not None and tick > 0:
        out_price = _floor_to_step(price, tick)
        decimals = max(0, -int(round(math.log10(tick)))) if tick < 1 else 0
        out_price = round(out_price, decimals)

    return qty, out_price



# ── Inventory preload (S68 P4) ───────────────────────────────────────────────
# A grid needs both base and quote assets before activation: BUY levels
# consume quote, SELL levels consume base. Cold-starting with only quote
# means only BUYs can be placed, leaving the upper half of the ladder
# un-backed. preload_base_inventory() solves this by market-buying the
# shortfall at entry so the full ladder can be placed.
#
# PRELOAD_MAX_USD is a safety cap — if the calculated preload exceeds it,
# we fail activation rather than spending more than the operator expects.

PRELOAD_MAX_USD = float(os.getenv("GRID_PRELOAD_MAX_USD", "400"))


def _get_free_balance(asset: str) -> float:
    """Return free (unlocked) balance for the given asset on Binance.US.
    Returns 0.0 on any API error — caller must treat that as no inventory."""
    try:
        acct = binance.get_account()
        for b in acct.get("balances", []):
            if b.get("asset", "").upper() == asset.upper():
                return float(b.get("free", 0) or 0)
    except Exception as exc:
        log.warning("get_free_balance(%s) failed: %s", asset, exc)
    return 0.0


def _split_pair(symbol: str) -> tuple[str, str]:
    """Extract (base, quote) from a spot symbol like BTCUSD or ETHUSDT.
    Uses exchange info so we do not hardcode asset assumptions."""
    try:
        info = binance.get_exchange_info(symbol.upper())
        syms = info.get("symbols", [])
        if syms:
            return syms[0].get("baseAsset", ""), syms[0].get("quoteAsset", "")
    except Exception as exc:
        log.warning("get_exchange_info(%s) failed: %s", symbol, exc)
    s = symbol.upper()
    if s.endswith("USDT"):
        return s[:-4], "USDT"
    if s.endswith("USDC"):
        return s[:-4], "USDC"
    if s.endswith("USD"):
        return s[:-3], "USD"
    return s, ""


def preload_base_inventory(symbol: str, levels: list, entry_price: float) -> tuple[bool, str]:
    """Ensure the account holds enough base asset to back every SELL level.

    If insufficient, places a MARKET BUY for the shortfall. Returns
    (True, message) on success (including "already sufficient") or
    (False, error_reason) on any failure that should block activation."""
    base, quote = _split_pair(symbol)
    if not base or not quote:
        return False, f"Could not parse base/quote from {symbol}"

    sell_qty_needed = sum(lvl.quantity for lvl in levels if lvl.side == "SELL")
    if sell_qty_needed <= 0:
        return True, "No SELL levels — preload not needed"

    free_base = _get_free_balance(base)
    shortfall = sell_qty_needed - free_base
    if shortfall <= 0:
        return True, f"Sufficient {base} inventory ({free_base:.8f} >= {sell_qty_needed:.8f})"

    # S68 P4 fee headroom: Binance.US spot commission (~0.1%) is taken from
    # the base asset on MARKET BUY. Without headroom, post-fee BTC falls
    # slightly short of sell_qty_needed and the highest SELL level rejects
    # with insufficient balance. Bump by 1% to cover fee + rounding noise.
    shortfall_with_headroom = shortfall * 1.01

    # Quantize the headroom-adjusted shortfall to LOT_SIZE so the market buy
    # is acceptable.
    q = _quantize_order(symbol, shortfall_with_headroom, None)
    if q is None:
        return False, (
            f"Preload shortfall {shortfall:.8f} {base} is below minQty after "
            f"quantization — cannot preload"
        )
    preload_qty, _ = q
    # _quantize_order floors. If that drops us below the actual need, bump
    # up by one step so we always cover (never under-preload).
    f = _get_symbol_filters(symbol)
    step = f.get("step_size", 0) or 0
    min_qty = f.get("min_qty", 0) or 0
    if step > 0 and preload_qty < shortfall_with_headroom:
        preload_qty = preload_qty + step
        if step < 1:
            decimals = max(0, -int(round(math.log10(step))))
            preload_qty = round(preload_qty, decimals)
    if min_qty > 0 and preload_qty < min_qty:
        preload_qty = min_qty

    est_cost_usd = preload_qty * entry_price

    if est_cost_usd > PRELOAD_MAX_USD:
        return False, (
            f"Preload cost estimate ${est_cost_usd:.2f} exceeds "
            f"GRID_PRELOAD_MAX_USD ${PRELOAD_MAX_USD:.2f}. "
            f"Raise the cap in .env or reduce grid allocation."
        )

    free_quote = _get_free_balance(quote)
    if free_quote < est_cost_usd * 1.01:
        return False, (
            f"Insufficient {quote} balance: have ${free_quote:.2f}, "
            f"need ~${est_cost_usd * 1.01:.2f} for preload. "
            f"Fund the account and retry."
        )

    log.info(
        "Preload: buying %.8f %s (~$%.2f %s, entry=$%.2f) to back %d SELL levels",
        preload_qty, base, est_cost_usd, quote, entry_price,
        sum(1 for lvl in levels if lvl.side == "SELL"),
    )

    try:
        result = binance.place_order(symbol, "BUY", "MARKET", preload_qty)
    except Exception as exc:
        return False, f"Preload MARKET BUY failed: {exc}"

    filled_qty = float(result.get("executedQty", 0) or 0)
    cumm_quote = float(result.get("cummulativeQuoteQty", 0) or 0)
    avg_price = (cumm_quote / filled_qty) if filled_qty > 0 else 0.0

    if filled_qty <= 0:
        return False, f"Preload order placed but did not fill: {result}"

    log.info(
        "Preload filled: %.8f %s at avg $%.2f (total $%.2f). orderId=%s",
        filled_qty, base, avg_price, cumm_quote, result.get("orderId"),
    )

    return True, (
        f"Preloaded {filled_qty:.8f} {base} at avg ${avg_price:,.2f} "
        f"(total ${cumm_quote:,.2f})"
    )


# ── State ─────────────────────────────────────────────────────────────────────

class GridState(Enum):
    STANDBY = "standby"
    ACTIVE  = "active"
    PAUSED  = "paused"


@dataclass
class GridLevel:
    price:    float
    side:     str           # "BUY" or "SELL"
    quantity: float
    order_id: Optional[int] = None
    filled:   bool          = False


@dataclass
class GridSession:
    db_id:        int
    pair:         str
    entry_price:  float
    upper_bound:  float
    lower_bound:  float
    levels:       list[GridLevel] = field(default_factory=list)
    state:        GridState       = GridState.STANDBY
    total_profit: float           = 0.0
    cycles:       int             = 0


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_grid_tables() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS grid_sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pair         TEXT    NOT NULL,
                entry_price  REAL    NOT NULL,
                upper_bound  REAL    NOT NULL,
                lower_bound  REAL    NOT NULL,
                num_levels   INTEGER NOT NULL,
                allocation   REAL    NOT NULL,
                total_profit REAL    DEFAULT 0,
                cycles       INTEGER DEFAULT 0,
                state        TEXT    DEFAULT 'standby',
                started_at   TEXT    DEFAULT (datetime('now')),
                stopped_at   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS grid_fills (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   INTEGER NOT NULL,
                pair         TEXT    NOT NULL,
                buy_price    REAL    NOT NULL,
                sell_price   REAL    NOT NULL,
                quantity     REAL    NOT NULL,
                net_profit   REAL    NOT NULL,
                filled_at    TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def db_create_session(pair: str, entry: float, upper: float, lower: float) -> int:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("""
            INSERT INTO grid_sessions
              (pair, entry_price, upper_bound, lower_bound, num_levels, allocation)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (pair, entry, upper, lower, GRID_NUM_LEVELS, GRID_ALLOCATION))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def db_update_session(session_id: int, state: str,
                       total_profit: float, cycles: int,
                       stopped: bool = False) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        if stopped:
            conn.execute("""
                UPDATE grid_sessions
                SET state=?, total_profit=?, cycles=?, stopped_at=datetime('now')
                WHERE id=?
            """, (state, total_profit, cycles, session_id))
        else:
            conn.execute("""
                UPDATE grid_sessions SET state=?, total_profit=?, cycles=? WHERE id=?
            """, (state, total_profit, cycles, session_id))
        conn.commit()
    finally:
        conn.close()


def db_log_fill(session_id: int, pair: str, buy_price: float,
                 sell_price: float, quantity: float) -> float:
    net = round((sell_price - buy_price) * quantity, 6)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            INSERT INTO grid_fills
              (session_id, pair, buy_price, sell_price, quantity, net_profit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session_id, pair, buy_price, sell_price, quantity, net))
        conn.commit()
    finally:
        conn.close()
    return net


def db_get_session_summary(session_id: int) -> dict:
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = conn.execute("""
            SELECT pair, entry_price, upper_bound, lower_bound,
                   total_profit, cycles, state, started_at
            FROM grid_sessions WHERE id=?
        """, (session_id,)).fetchone()
        conn.close()
        if not row:
            return {}
        return {
            "pair": row[0], "entry_price": row[1],
            "upper_bound": row[2], "lower_bound": row[3],
            "total_profit": row[4], "cycles": row[5],
            "state": row[6], "started_at": row[7],
        }
    except Exception as exc:
        log.error("db_get_session_summary: %s", exc)
        return {}


# ── RSI + trend guard ─────────────────────────────────────────────────────────

def _fetch_klines(symbol: str, interval: str = "4h", limit: int = RSI_CANDLE_LIMIT) -> list[float]:
    """
    Fetch closing prices from Binance.US public klines endpoint.
    Returns list of close prices, newest last.
    """
    try:
        resp = requests.get(
            f"{binance.BASE_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        return [float(k[4]) for k in resp.json()]   # index 4 = close price
    except Exception as exc:
        log.warning("klines fetch failed for %s: %s", symbol, exc)
        return []


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI. Returns 50.0 on insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def trend_guard(symbol: str) -> tuple[bool, float]:
    """
    Returns (blocked, rsi).
    blocked=True means strong trend detected — grid should stay in STANDBY.
    """
    if not GRID_RSI_GUARD:
        return False, 50.0
    closes = _fetch_klines(symbol)
    rsi    = calculate_rsi(closes)
    blocked = rsi > RSI_UPPER_BLOCK or rsi < RSI_LOWER_BLOCK
    log.info("Trend guard %s: RSI=%.1f blocked=%s", symbol, rsi, blocked)
    return blocked, rsi


# ── Grid math ─────────────────────────────────────────────────────────────────

def calculate_grid_levels(entry_price: float,
                           upper_pct: float = GRID_UPPER_PCT,
                           lower_pct: float = GRID_LOWER_PCT,
                           num_levels: int   = GRID_NUM_LEVELS,
                           allocation: float = GRID_ALLOCATION) -> tuple[list[GridLevel], float, float]:
    """
    Build the full grid ladder.

    Returns:
        (levels, upper_bound, lower_bound)
        levels is a list of GridLevel objects sorted price ascending.

    Each grid cell receives an equal allocation. Buys are placed below entry,
    sells are placed above entry. The quantity per level is calculated so that
    each buy order costs approximately allocation / num_levels USD.
    """
    upper = round(entry_price * (1 + upper_pct / 100), 8)
    lower = round(entry_price * (1 - lower_pct / 100), 8)
    step  = (upper - lower) / num_levels
    qty_per_level = round((allocation / num_levels) / entry_price, 6)

    levels: list[GridLevel] = []
    for i in range(num_levels + 1):
        price = round(lower + i * step, 2)
        side  = "BUY" if price < entry_price else "SELL"
        levels.append(GridLevel(price=price, side=side, quantity=qty_per_level))

    log.info(
        "Grid levels: entry=%.2f upper=%.2f lower=%.2f "
        "levels=%d qty_each=%.6f",
        entry_price, upper, lower, len(levels), qty_per_level,
    )
    return levels, upper, lower


# ── Order management ──────────────────────────────────────────────────────────

def place_grid_orders(session: GridSession) -> int:
    """
    Place limit orders for all unfilled levels.
    Returns count of successfully placed orders.

    S68 P1: quantize qty to LOT_SIZE stepSize and price to PRICE_FILTER
    tickSize before submission. Binance.US rejects non-quantized orders
    with -1013 "Filter failure: LOT_SIZE".
    """
    placed = 0
    for lvl in session.levels:
        if lvl.order_id or lvl.filled:
            continue
        q = _quantize_order(session.pair, lvl.quantity, lvl.price)
        if q is None:
            continue
        qty_q, price_q = q
        try:
            result = binance.place_order(
                symbol     = session.pair,
                side       = lvl.side,
                order_type = "LIMIT",
                quantity   = qty_q,
                price      = price_q,
            )
            lvl.order_id = result.get("orderId")
            lvl.quantity = qty_q
            if price_q is not None:
                lvl.price = price_q
            placed += 1
            log.info("Grid order placed: %s %.8f @ %.2f orderId=%s",
                     lvl.side, qty_q, price_q or 0.0, lvl.order_id)
        except Exception as exc:
            log.warning("Failed to place grid order %.2f %s: %s",
                        lvl.price, lvl.side, exc)
    return placed


def cancel_all_grid_orders(session: GridSession) -> int:
    """Cancel every open grid order. Returns count cancelled."""
    cancelled = 0
    for lvl in session.levels:
        if not lvl.order_id:
            continue
        try:
            binance.cancel_order(session.pair, lvl.order_id)
            lvl.order_id = None
            cancelled += 1
        except Exception as exc:
            log.warning("Failed to cancel order %s: %s", lvl.order_id, exc)
    log.info("Cancelled %d grid orders for %s", cancelled, session.pair)
    return cancelled


def on_order_fill(session: GridSession, filled_level: GridLevel) -> Optional[GridLevel]:
    """
    Called when a level's order is confirmed filled.

    If a BUY filled  → place a SELL at the next level up.
    If a SELL filled → place a BUY at the next level down.

    Returns the newly placed counterpart GridLevel, or None if at boundary.
    Logs the completed cycle and updates session profit.
    """
    filled_level.filled   = True
    filled_level.order_id = None
    levels = sorted(session.levels, key=lambda x: x.price)
    idx    = next((i for i, l in enumerate(levels) if l.price == filled_level.price), None)
    if idx is None:
        return None

    if filled_level.side == "BUY":
        # Place sell at next level up
        if idx + 1 < len(levels):
            counter = levels[idx + 1]
            q = _quantize_order(session.pair, filled_level.quantity, counter.price)
            if q is None:
                log.warning("Counter SELL skipped - qty below minQty after quantize")
                return None
            qty_q, price_q = q
            # S68 P4: if the counter level already has a live order, cancel it
            # first so we never orphan an order on the exchange.
            if counter.order_id:
                try:
                    binance.cancel_order(session.pair, counter.order_id)
                    log.info("Cancelled stale order %s on counter level @ %.2f",
                             counter.order_id, counter.price)
                except Exception as exc:
                    log.warning("Failed to cancel stale counter order %s: %s — skipping counter SELL",
                                counter.order_id, exc)
                    return None
                counter.order_id = None
            try:
                result = binance.place_order(
                    session.pair, "SELL", "LIMIT",
                    qty_q, price_q,
                )
                counter.order_id = result.get("orderId")
                counter.filled   = False
                counter.quantity = qty_q
                if price_q is not None:
                    counter.price = price_q
                log.info("Counter SELL placed @ %.2f after BUY fill @ %.2f",
                         counter.price, filled_level.price)
                return counter
            except Exception as exc:
                log.warning("Counter SELL failed: %s", exc)
    else:
        # SELL filled → place buy at next level down, record cycle profit
        if idx - 1 >= 0:
            buy_level = levels[idx - 1]
            net = db_log_fill(
                session.db_id, session.pair,
                buy_level.price, filled_level.price, filled_level.quantity,
            )
            session.total_profit = round(session.total_profit + net, 6)
            session.cycles      += 1
            log.info("Cycle complete: buy=%.2f sell=%.2f qty=%.6f net=%.4f total=%.4f",
                     buy_level.price, filled_level.price,
                     filled_level.quantity, net, session.total_profit)
            q = _quantize_order(session.pair, filled_level.quantity, buy_level.price)
            if q is None:
                log.warning("Counter BUY skipped - qty below minQty after quantize")
                return None
            qty_q, price_q = q
            # S68 P4: cancel stale order on counter level before overwriting.
            if buy_level.order_id:
                try:
                    binance.cancel_order(session.pair, buy_level.order_id)
                    log.info("Cancelled stale order %s on counter level @ %.2f",
                             buy_level.order_id, buy_level.price)
                except Exception as exc:
                    log.warning("Failed to cancel stale counter order %s: %s — skipping counter BUY",
                                buy_level.order_id, exc)
                    return None
                buy_level.order_id = None
            try:
                result = binance.place_order(
                    session.pair, "BUY", "LIMIT",
                    qty_q, price_q,
                )
                buy_level.order_id = result.get("orderId")
                buy_level.filled   = False
                buy_level.quantity = qty_q
                if price_q is not None:
                    buy_level.price = price_q
                return buy_level
            except Exception as exc:
                log.warning("Counter BUY failed: %s", exc)
    return None


def monitor_boundary(session: GridSession, current_price: float) -> bool:
    """
    Returns True if price has exited the grid range.
    Caller should pause the engine when this returns True.
    """
    if current_price >= session.upper_bound or current_price <= session.lower_bound:
        log.warning(
            "Price %.2f outside grid range [%.2f, %.2f] — pausing",
            current_price, session.lower_bound, session.upper_bound,
        )
        return True
    return False


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


async def handle_grid_command(client: httpx.AsyncClient,
                               subcommand: str,
                               engine: "GridEngine") -> None:
    """
    Handle /grid Telegram commands.
      /grid status  → current state, price vs range, cycles, profit
      /grid start   → activate engine (subject to trend guard)
      /grid stop    → deactivate and cancel all orders
    """
    sub = subcommand.strip().lower()

    if sub == "start":
        result = engine.start()
        await _tg_send(client, result)
        return

    if sub == "stop":
        result = engine.stop()
        await _tg_send(client, result)
        return

    # Default: status
    await _tg_send(client, engine.status_message())


# ── Engine class ──────────────────────────────────────────────────────────────

class GridEngine:
    """
    Manages the full grid lifecycle.
    Instantiate once, call start() / stop() from Telegram commands or the main loop.
    """

    def __init__(self) -> None:
        self.session: Optional[GridSession] = None
        self._state = GridState.STANDBY
        ensure_grid_tables()

    @property
    def state(self) -> GridState:
        return self._state

    def start(self) -> str:
        """
        Attempt to activate the grid. Checks trend guard first.
        Returns a status string for Telegram.
        """
        if self._state == GridState.ACTIVE:
            return "Grid is already active."

        if not is_enabled():
            return "Grid trading is disabled. Enable from the Strategies page, or set GRID_ENABLED=true in .env."

        # Trend guard check
        blocked, rsi = trend_guard(GRID_PAIR)
        if blocked:
            direction = "overbought" if rsi > RSI_UPPER_BLOCK else "oversold"
            return (
                f"Trend guard blocked activation.\n"
                f"RSI(4h) = {rsi:.1f} ({direction}). "
                f"Grid requires RSI between {RSI_LOWER_BLOCK} and {RSI_UPPER_BLOCK}.\n"
                f"Try again when momentum normalizes."
            )

        # Fetch current price as entry
        try:
            ticker      = binance.get_ticker(GRID_PAIR)
            entry_price = float(ticker["lastPrice"])
        except Exception as exc:
            return f"Failed to fetch {GRID_PAIR} price: {exc}"

        levels, upper, lower = calculate_grid_levels(entry_price)

        # S68 P4: preload base asset inventory so the full ladder can place.
        # If the account holds only quote currency (e.g. $500 USD / 0 BTC),
        # market-buy the shortfall at entry to back all SELL levels.
        ok, preload_msg = preload_base_inventory(GRID_PAIR, levels, entry_price)
        if not ok:
            return f"Grid activation blocked — preload failed:\n{preload_msg}"
        log.info("Preload status: %s", preload_msg)

        session_id  = db_create_session(GRID_PAIR, entry_price, upper, lower)
        self.session = GridSession(
            db_id       = session_id,
            pair        = GRID_PAIR,
            entry_price = entry_price,
            upper_bound = upper,
            lower_bound = lower,
            levels      = levels,
        )
        self._state          = GridState.ACTIVE
        self.session.state   = GridState.ACTIVE

        placed = place_grid_orders(self.session)
        db_update_session(session_id, "active", 0.0, 0)

        # S68 P3: sync the DB flag so the panel reflects active state.
        # Done after successful order placement only — if start fails mid-way
        # we leave the flag alone so stale state does not persist.
        _set_enabled(True)

        log.info("Grid started: pair=%s entry=%.2f levels=%d placed=%d",
                 GRID_PAIR, entry_price, len(levels), placed)

        return (
            f"Grid ACTIVE\n\n"
            f"Pair:       {GRID_PAIR}\n"
            f"Entry:      ${entry_price:,.2f}\n"
            f"Range:      ${lower:,.2f} — ${upper:,.2f}\n"
            f"Levels:     {GRID_NUM_LEVELS}\n"
            f"Allocation: ${GRID_ALLOCATION:,.0f}\n"
            f"RSI(4h):    {rsi:.1f}\n"
            f"Preload:    {preload_msg}\n"
            f"Orders placed: {placed}"
        )

    def stop(self) -> str:
        """Deactivate the grid and cancel all open orders."""
        if self._state == GridState.STANDBY:
            return "Grid is not active."

        cancelled = 0
        if self.session:
            cancelled = cancel_all_grid_orders(self.session)
            db_update_session(
                self.session.db_id, "stopped",
                self.session.total_profit, self.session.cycles,
                stopped=True,
            )
            log.info("Grid stopped: session_id=%d profit=%.4f cycles=%d",
                     self.session.db_id, self.session.total_profit,
                     self.session.cycles)

        profit = self.session.total_profit if self.session else 0.0
        cycles = self.session.cycles       if self.session else 0
        self._state  = GridState.STANDBY
        self.session = None

        # S68 P3: sync the DB flag so the panel reflects standby state.
        _set_enabled(False)

        return (
            f"Grid STOPPED\n\n"
            f"Orders cancelled: {cancelled}\n"
            f"Cycles completed: {cycles}\n"
            f"Total profit:     ${profit:.4f}"
        )

    def status_message(self) -> str:
        if self._state == GridState.STANDBY or not self.session:
            blocked, rsi = trend_guard(GRID_PAIR)
            guard_str    = f"RSI(4h)={rsi:.1f} — {'BLOCKED' if blocked else 'OK'}"
            return (
                f"Grid STANDBY\n\n"
                f"Pair:       {GRID_PAIR}\n"
                f"Trend guard: {guard_str}\n\n"
                f"Send /grid start to activate."
            )

        try:
            ticker = binance.get_ticker(self.session.pair)
            price  = float(ticker["lastPrice"])
        except Exception:
            price = 0.0

        pct_in_range = ""
        span = self.session.upper_bound - self.session.lower_bound
        if span > 0 and price > 0:
            pos = (price - self.session.lower_bound) / span * 100
            pct_in_range = f" ({pos:.0f}% up from lower)"

        blocked, rsi = trend_guard(self.session.pair)

        return (
            f"Grid {self._state.value.upper()}\n\n"
            f"Pair:     {self.session.pair}\n"
            f"Price:    ${price:,.2f}{pct_in_range}\n"
            f"Range:    ${self.session.lower_bound:,.2f} — "
            f"${self.session.upper_bound:,.2f}\n"
            f"RSI(4h):  {rsi:.1f}"
            f"{'  ⚠️ trend guard active' if blocked else ''}\n"
            f"Cycles:   {self.session.cycles}\n"
            f"Profit:   ${self.session.total_profit:.4f}\n"
            f"Entry:    ${self.session.entry_price:,.2f}"
        )

    async def check_fills(self) -> None:
        """
        Poll Binance for filled orders and process them.
        Called on each tick of the main loop.
        """
        if self._state != GridState.ACTIVE or not self.session:
            return
        try:
            open_order_ids = {
                o["orderId"] for o in binance.get_open_orders(self.session.pair)
            }
        except Exception as exc:
            log.warning("get_open_orders failed: %s", exc)
            return

        for lvl in self.session.levels:
            if lvl.order_id and lvl.order_id not in open_order_ids and not lvl.filled:
                log.info("Fill detected: %s @ %.2f orderId=%s",
                         lvl.side, lvl.price, lvl.order_id)
                on_order_fill(self.session, lvl)
                db_update_session(
                    self.session.db_id, "active",
                    self.session.total_profit, self.session.cycles,
                )

        # Boundary check
        try:
            ticker = binance.get_ticker(self.session.pair)
            price  = float(ticker["lastPrice"])
            if monitor_boundary(self.session, price):
                self._state = GridState.PAUSED
                cancel_all_grid_orders(self.session)
                db_update_session(
                    self.session.db_id, "paused",
                    self.session.total_profit, self.session.cycles,
                )
                log.warning("Grid paused — price exited range")
        except Exception as exc:
            log.warning("Boundary check failed: %s", exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def grid_loop(engine: GridEngine) -> None:
    """
    Runs alongside scanner. Checks fills and boundary every POLL_INTERVAL seconds.
    Also re-evaluates trend guard every 4 hours when in PAUSED state.
    """
    # Engine loop runs forever. is_enabled() is checked each tick so the
    # panel can toggle the strategy on/off at runtime without a restart.
    log.info("Grid engine loop started (pair=%s interval=%ds, initially_enabled=%s)",
             GRID_PAIR, POLL_INTERVAL, is_enabled())
    last_rsi_check = 0.0
    _was_enabled = None  # tracks enabled state across ticks for transition logging

    async with httpx.AsyncClient() as client:
        while True:
            # Runtime enable check
            _now_enabled = is_enabled()
            if _now_enabled != _was_enabled:
                if _now_enabled:
                    log.info("Grid engine ENABLED — resuming trading logic")
                    # S68 P3: on false->true transition, if engine is STANDBY
                    # (no live session), activate it in this process. This is
                    # the panel-driven activation path — flag flipped in DB,
                    # scanner picks it up, session created on the singleton
                    # that grid_loop manages. Avoids the MCP process boundary.
                    if engine.state == GridState.STANDBY:
                        try:
                            result = engine.start()
                            log.info("Grid auto-start via flag transition: %s",
                                     result.splitlines()[0] if result else "(no message)")
                            await _tg_send(client, f"Grid activated from panel.\n\n{result}")
                        except Exception as exc:
                            log.error("Auto-start failed: %s", exc)
                            await _tg_send(client, f"Grid auto-start failed: {exc}")
                else:
                    log.info("Grid engine DISABLED — standing by (toggle from panel to resume)")
                    # S68 P3: on true->false transition, if engine is ACTIVE,
                    # stop it in this process so orders are cancelled cleanly.
                    if engine.state == GridState.ACTIVE:
                        try:
                            result = engine.stop()
                            log.info("Grid auto-stop via flag transition: %s",
                                     result.splitlines()[0] if result else "(no message)")
                            await _tg_send(client, f"Grid deactivated from panel.\n\n{result}")
                        except Exception as exc:
                            log.error("Auto-stop failed: %s", exc)
                            await _tg_send(client, f"Grid auto-stop failed: {exc}")
                _was_enabled = _now_enabled
            if not _now_enabled:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            try:
                await engine.check_fills()

                # If paused, try to re-activate every 4 hours when trend clears
                if engine.state == GridState.PAUSED:
                    now = time.time()
                    if now - last_rsi_check > 14400:   # 4 hours
                        last_rsi_check = now
                        blocked, rsi   = trend_guard(GRID_PAIR)
                        if not blocked and engine.session:
                            engine._state = GridState.ACTIVE
                            placed = place_grid_orders(engine.session)
                            db_update_session(
                                engine.session.db_id, "active",
                                engine.session.total_profit, engine.session.cycles,
                            )
                            await _tg_send(
                                client,
                                f"Grid re-activated after pause.\n"
                                f"RSI(4h)={rsi:.1f} — trend guard cleared.\n"
                                f"Orders placed: {placed}",
                            )
                            log.info("Grid re-activated: rsi=%.1f placed=%d", rsi, placed)

            except Exception as exc:
                log.error("Grid loop error: %s", exc)

            await asyncio.sleep(POLL_INTERVAL)


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ensure_grid_tables()
    print(f"Grid engine self-test | pair={GRID_PAIR} "
          f"upper={GRID_UPPER_PCT}% lower={GRID_LOWER_PCT}% "
          f"levels={GRID_NUM_LEVELS} alloc=${GRID_ALLOCATION}\n")

    # Trend guard check
    blocked, rsi = trend_guard(GRID_PAIR)
    print(f"RSI(4h): {rsi:.1f} — {'BLOCKED (strong trend)' if blocked else 'OK (grid safe to activate)'}")

    # Grid math preview (no orders placed)
    try:
        ticker = binance.get_ticker(GRID_PAIR)
        price  = float(ticker["lastPrice"])
        print(f"Current {GRID_PAIR}: ${price:,.2f}")
        levels, upper, lower = calculate_grid_levels(price)
        buys  = [l for l in levels if l.side == "BUY"]
        sells = [l for l in levels if l.side == "SELL"]
        print(f"Grid: ${lower:,.2f} — ${upper:,.2f}")
        print(f"Buy levels:  {len(buys)}  |  Sell levels: {len(sells)}")
        print(f"Qty per level: {levels[0].quantity:.6f} {GRID_PAIR[:3]}")
        print(f"Grid spacing: ${(upper - lower) / GRID_NUM_LEVELS:,.2f}")
    except Exception as exc:
        print(f"Ticker fetch failed (API keys not set?): {exc}")
        print("Grid math requires live price — configure BINANCE_API_KEY in .env")
