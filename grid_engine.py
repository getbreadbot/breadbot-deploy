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
    """
    placed = 0
    for lvl in session.levels:
        if lvl.order_id or lvl.filled:
            continue
        try:
            result = binance.place_order(
                symbol     = session.pair,
                side       = lvl.side,
                order_type = "LIMIT",
                quantity   = lvl.quantity,
                price      = lvl.price,
            )
            lvl.order_id = result.get("orderId")
            placed += 1
            log.info("Grid order placed: %s %.6f @ %.2f orderId=%s",
                     lvl.side, lvl.quantity, lvl.price, lvl.order_id)
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
            try:
                result = binance.place_order(
                    session.pair, "SELL", "LIMIT",
                    filled_level.quantity, counter.price,
                )
                counter.order_id = result.get("orderId")
                counter.filled   = False
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
            try:
                result = binance.place_order(
                    session.pair, "BUY", "LIMIT",
                    filled_level.quantity, buy_level.price,
                )
                buy_level.order_id = result.get("orderId")
                buy_level.filled   = False
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

        if not GRID_ENABLED:
            return "Grid trading is disabled. Set GRID_ENABLED=true in .env to enable."

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
    if not GRID_ENABLED:
        log.info("Grid engine disabled (GRID_ENABLED=false)")
        return

    log.info("Grid engine loop started (pair=%s interval=%ds)", GRID_PAIR, POLL_INTERVAL)
    last_rsi_check = 0.0

    async with httpx.AsyncClient() as client:
        while True:
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
