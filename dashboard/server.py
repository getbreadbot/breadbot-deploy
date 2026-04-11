from typing import Optional
"""
dashboard/server.py — Breadbot Terminal API + Static File Server
GET endpoints: read-only queries
POST endpoints: trade execution, bot control, alert decisions
Run: python server.py  →  http://localhost:8000
"""

import json
import csv
import io
import os
import sqlite3
import sys
import asyncio
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Load .env so BASESCAN_API_KEY and other optional vars are available
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import httpx
from fastapi import FastAPI, Query, HTTPException, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

DB_PATH = Path(__file__).parent.parent / "data" / "cryptobot.db"
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from alt_data_signals import get_cached_signals as _get_signals
except (ImportError, Exception):
    _get_signals = None

# ── Wallet addresses ─────────────────────────────────────────────────────────
BASE_WALLET = "0x9EaC5E219d6a4Be6Ab539d0BDE954dDd4c20B924"
SOLANA_WALLET = "6LW6H6GguLQm7u1wzNaCtvy1sqjhoHxwQhSBV1ynPHy8"
FLASH_LOAN_CONTRACT = "0x60b30eb32656dfDA6Aed6fd0c073fe872717d357"
BASE_USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ── Caches ────────────────────────────────────────────────────────────────────
_price_cache = {"data": {}, "ts": 0}
_balance_cache = {"data": {}, "ts": 0}

# ── ERC-20 balanceOf ABI (minimal) ───────────────────────────────────────────
ERC20_BALANCE_ABI = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]

app = FastAPI(title="Breadbot Terminal", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db(readonly=True):
    if not DB_PATH.exists():
        return None
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def get_db_rw():
    """Read-write connection — used only for action endpoints."""
    conn = sqlite3.connect(str(DB_PATH), timeout=2)
    conn.row_factory = sqlite3.Row
    _ensure_bot_config(conn)
    return conn


def _ensure_bot_config(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO bot_config (key, value)
        VALUES ('trading_paused', '0'), ('pause_reason', '')
    """)
    conn.commit()


def rows_to_dicts(rows) -> list:
    return [dict(row) for row in rows] if rows else []


# ── GET: Status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    empty = {
        "trading_active": False, "trading_paused": False, "pause_reason": "",
        "daily_pnl": 0.0, "daily_loss_pct_used": 0.0, "open_positions": 0,
        "last_scan": None, "alerts_today": 0, "buys_today": 0,
        "skips_today": 0, "db_exists": False,
    }
    conn = get_db()
    if not conn:
        return JSONResponse(empty)
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) as p FROM trades WHERE date(executed_at)=?", (today,)
        ).fetchone()["p"]
        open_pos = conn.execute(
            "SELECT COUNT(*) as c FROM positions WHERE status='open'"
        ).fetchone()["c"]
        alerts = conn.execute(
            "SELECT COUNT(*) as c FROM meme_alerts WHERE date(created_at)=?", (today,)
        ).fetchone()["c"]
        buys = conn.execute(
            "SELECT COUNT(*) as c FROM meme_alerts WHERE date(created_at)=? AND decision='buy'", (today,)
        ).fetchone()["c"]
        skips = conn.execute(
            "SELECT COUNT(*) as c FROM meme_alerts WHERE date(created_at)=? AND decision='skip'", (today,)
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT created_at FROM meme_alerts ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        # Bot config (pause state)
        try:
            paused_row = conn.execute("SELECT value FROM bot_config WHERE key='trading_paused'").fetchone()
            reason_row = conn.execute("SELECT value FROM bot_config WHERE key='pause_reason'").fetchone()
            is_paused  = paused_row and paused_row["value"] == "1"
            reason     = reason_row["value"] if reason_row else ""
        except Exception:
            is_paused, reason = False, ""
        # Portfolio utilization: sum of open position cost bases / assumed total portfolio
        total_cost = conn.execute(
            "SELECT COALESCE(SUM(cost_basis_usd),0) as s FROM positions WHERE status='open'"
        ).fetchone()["s"]
        total_portfolio = 5000.0  # assumed total portfolio value
        port_util = round(total_cost / total_portfolio * 100, 1) if total_portfolio > 0 else 0

        return JSONResponse({
            "trading_active": not is_paused, "trading_paused": is_paused, "pause_reason": reason,
            "daily_pnl": round(pnl, 2), "daily_loss_pct_used": 0.0, "open_positions": open_pos,
            "portfolio_utilization": port_util,
            "last_scan": last["created_at"] if last else None,
            "alerts_today": alerts, "buys_today": buys, "skips_today": skips, "db_exists": True,
        })
    finally:
        conn.close()


# ── GET: Positions ────────────────────────────────────────────────────────────

@app.get("/api/positions")
def api_positions():
    conn = get_db()
    if not conn:
        return JSONResponse({"open": [], "closed": []})
    try:
        open_r   = conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
        closed_r = conn.execute("SELECT * FROM positions WHERE status!='open' ORDER BY closed_at DESC LIMIT 30").fetchall()
        return JSONResponse({"open": rows_to_dicts(open_r), "closed": rows_to_dicts(closed_r)})
    finally:
        conn.close()


# ── GET: Alerts ───────────────────────────────────────────────────────────────

@app.get("/api/alerts")
def api_alerts(days: int = Query(default=7, ge=1, le=30), decision: str = Query(default="")):
    conn = get_db()
    if not conn:
        return JSONResponse({"alerts": []})
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        if decision:
            rows = conn.execute(
                "SELECT * FROM meme_alerts WHERE created_at>? AND decision=? ORDER BY created_at DESC",
                (cutoff, decision)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM meme_alerts WHERE created_at>? ORDER BY created_at DESC", (cutoff,)
            ).fetchall()
        alerts = rows_to_dicts(rows)
        for a in alerts:
            for fld in ("rug_flags", "flags"):
                if a.get(fld) and isinstance(a[fld], str):
                    try:
                        a[fld] = json.loads(a[fld])
                    except Exception:
                        a[fld] = [f for f in a[fld].split(",") if f]
        return JSONResponse({"alerts": alerts})
    finally:
        conn.close()


# ── GET: Yields ───────────────────────────────────────────────────────────────

@app.get("/api/yields")
def api_yields():
    conn = get_db()
    if not conn:
        return JSONResponse({"current": [], "history": {}})
    try:
        current = conn.execute("""
            SELECT platform, asset, apy, tvl_usd, notes, recorded_at
            FROM yield_snapshots y1
            WHERE recorded_at=(
                SELECT MAX(recorded_at) FROM yield_snapshots y2
                WHERE y2.platform=y1.platform AND y2.asset=y1.asset
            )
            ORDER BY apy DESC
        """).fetchall()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        hist = conn.execute(
            "SELECT platform, asset, apy, recorded_at FROM yield_snapshots WHERE recorded_at>? ORDER BY platform, recorded_at ASC",
            (cutoff,)
        ).fetchall()
        history = {}
        for r in hist:
            k = r["platform"]
            if k not in history:
                history[k] = []
            history[k].append({"apy": r["apy"], "date": r["recorded_at"]})
        return JSONResponse({"current": rows_to_dicts(current), "history": history})
    finally:
        conn.close()


# ── GET: Trades ───────────────────────────────────────────────────────────────

@app.get("/api/trades")
def api_trades():
    conn = get_db()
    if not conn:
        return JSONResponse({"trades": [], "cumulative_pnl": []})
    try:
        trades = conn.execute("""
            SELECT t.*, p.symbol, p.chain FROM trades t
            LEFT JOIN positions p ON t.position_id=p.id
            ORDER BY t.executed_at DESC
        """).fetchall()
        pnl_rows = conn.execute("""
            SELECT date(executed_at) as d, SUM(COALESCE(pnl_usd,0)) as dp
            FROM trades GROUP BY date(executed_at) ORDER BY d ASC
        """).fetchall()
        cumulative = []
        total = 0.0
        for r in pnl_rows:
            total += r["dp"] or 0
            cumulative.append({"date": r["d"], "pnl": round(total, 2)})
        return JSONResponse({"trades": rows_to_dicts(trades), "cumulative_pnl": cumulative})
    finally:
        conn.close()


# ── POST: Action models ───────────────────────────────────────────────────────

class AlertDecisionBody(BaseModel):
    alert_id: int
    decision: str   # "buy" or "skip"

class ClosePositionBody(BaseModel):
    position_id: int
    reason: str = "manual"

class PauseBody(BaseModel):
    reason: str = "Paused from dashboard"


# ── POST: Buy / Skip alert ────────────────────────────────────────────────────

@app.post("/api/action/decision")
def action_decision(body: AlertDecisionBody):
    """Mark an alert as buy or skip. Buy also attempts live execution if connector is available."""
    if body.decision not in ("buy", "skip"):
        raise HTTPException(400, "decision must be 'buy' or 'skip'")

    conn = get_db_rw()
    try:
        row = conn.execute("SELECT * FROM meme_alerts WHERE id=?", (body.alert_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Alert {body.alert_id} not found")

        row = dict(row)
        if row.get("decision") not in (None, "", "pending"):
            return JSONResponse({"ok": False, "msg": f"Alert already {row['decision']}"})

        conn.execute("UPDATE meme_alerts SET decision=? WHERE id=?", (body.decision, body.alert_id))

        if body.decision == "buy":
            # Write a pending trade to DB — main.py picks this up and executes
            symbol   = row.get("symbol", "")
            chain    = row.get("chain", "")
            size_usd = row.get("position_size_usd") or row.get("size_usd") or 0
            price    = row.get("price_usd") or 0
            stop     = round(float(price) * 0.935, 8) if price else 0
            target   = round(float(price) * 1.15,  8) if price else 0
            conn.execute("""
                INSERT OR IGNORE INTO positions
                    (symbol, chain, token_name, entry_price, cost_basis_usd, stop_loss_usd, take_profit_25, status)
                VALUES (?,?,?,?,?,?,?,'open')
            """, (symbol, chain, row.get("token_name",""), price, size_usd, stop, target))

        conn.commit()
        return JSONResponse({"ok": True, "decision": body.decision, "alert_id": body.alert_id})
    finally:
        conn.close()


# ── POST: Pause / Resume ──────────────────────────────────────────────────────

@app.post("/api/action/pause")
def action_pause(body: PauseBody):
    conn = get_db_rw()
    try:
        conn.execute("UPDATE bot_config SET value='1', updated_at=datetime('now') WHERE key='trading_paused'")
        conn.execute("UPDATE bot_config SET value=?, updated_at=datetime('now') WHERE key='pause_reason'", (body.reason,))
        conn.commit()
        return JSONResponse({"ok": True, "paused": True})
    finally:
        conn.close()


@app.post("/api/action/resume")
def action_resume():
    conn = get_db_rw()
    try:
        conn.execute("UPDATE bot_config SET value='0', updated_at=datetime('now') WHERE key='trading_paused'")
        conn.execute("UPDATE bot_config SET value='', updated_at=datetime('now') WHERE key='pause_reason'")
        conn.commit()
        return JSONResponse({"ok": True, "paused": False})
    finally:
        conn.close()


# ── POST: Close position ──────────────────────────────────────────────────────

@app.post("/api/action/close-position")
def action_close_position(body: ClosePositionBody):
    conn = get_db_rw()
    try:
        row = conn.execute("SELECT * FROM positions WHERE id=?", (body.position_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Position not found")
        if dict(row).get("status") != "open":
            return JSONResponse({"ok": False, "msg": "Position is not open"})
        conn.execute(
            "UPDATE positions SET status=?, closed_at=datetime('now') WHERE id=?",
            (body.reason, body.position_id)
        )
        conn.commit()
        return JSONResponse({"ok": True, "position_id": body.position_id, "status": body.reason})
    finally:
        conn.close()


# ── POST: Force scan (writes a flag to DB; main.py checks for it) ─────────────

@app.post("/api/action/force-scan")
def action_force_scan():
    conn = get_db_rw()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO bot_config (key, value, updated_at)
            VALUES ('force_scan', '1', datetime('now'))
        """)
        conn.commit()
        return JSONResponse({"ok": True, "msg": "Force scan queued. Bot will scan on next cycle."})
    finally:
        conn.close()


# ── POST: Reset daily counters ────────────────────────────────────────────────

@app.post("/api/tour-complete")
def api_tour_complete():
    """Mark the onboarding tour as complete so it doesn't re-fire."""
    conn = get_db_rw()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO bot_config (key, value, updated_at)
            VALUES ('tour_complete', '1', datetime('now'))
        """)
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.post("/api/action/reset-daily")
def action_reset_daily():
    """Reset daily counters by clearing today's decision data and loss tracking."""
    conn = get_db_rw()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Reset the daily loss tracking flag
        conn.execute("""
            INSERT OR REPLACE INTO bot_config (key, value, updated_at)
            VALUES ('daily_loss_reset', ?, datetime('now'))
        """, (today,))
        conn.commit()
        return JSONResponse({"ok": True, "msg": "Daily counters reset. Changes take effect on next cycle."})
    finally:
        conn.close()


# ── GET: Bot config (for Controls page) ──────────────────────────────────────

@app.get("/api/bot-config")
def api_bot_config():
    conn = get_db()
    if not conn:
        return JSONResponse({"paused": False, "pause_reason": "", "force_scan": False})
    try:
        try:
            rows = conn.execute("SELECT key, value FROM bot_config").fetchall()
            cfg  = {r["key"]: r["value"] for r in rows}
        except Exception:
            cfg = {}
        return JSONResponse({
            "paused":       cfg.get("trading_paused", "0") == "1",
            "pause_reason": cfg.get("pause_reason", ""),
            "force_scan":   cfg.get("force_scan", "0") == "1",
        })
    finally:
        conn.close()


# ── GET: Summary stats for Controls page ─────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    conn = get_db()
    if not conn:
        return JSONResponse({})
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_trades  = conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()["c"]
        win_trades    = conn.execute("SELECT COUNT(*) as c FROM trades WHERE pnl_usd>0").fetchone()["c"]
        total_pnl     = conn.execute("SELECT COALESCE(SUM(pnl_usd),0) as s FROM trades").fetchone()["s"]
        total_alerts  = conn.execute("SELECT COUNT(*) as c FROM meme_alerts").fetchone()["c"]
        buy_decisions = conn.execute("SELECT COUNT(*) as c FROM meme_alerts WHERE decision='buy'").fetchone()["c"]
        open_pos      = conn.execute("SELECT COUNT(*) as c FROM positions WHERE status='open'").fetchone()["c"]
        best_yield    = conn.execute("SELECT MAX(apy) as m FROM yield_snapshots").fetchone()["m"]
        return JSONResponse({
            "total_trades": total_trades,
            "win_rate": round(win_trades / total_trades * 100, 1) if total_trades else 0,
            "total_pnl": round(total_pnl, 2),
            "total_alerts": total_alerts,
            "buy_rate": round(buy_decisions / total_alerts * 100, 1) if total_alerts else 0,
            "open_positions": open_pos,
            "best_yield_apy": round(best_yield, 2) if best_yield else 0,
        })
    finally:
        conn.close()




# ── GET: Alt Data Signals ─────────────────────────────────────────────────────

@app.get("/api/signals")
def api_signals():
    """Read latest alt-data signals from DB and return structured payload for dashboard."""
    conn = get_db()
    if not conn:
        return JSONResponse({"error": "no db"})
    try:
        # Pull latest value per (source, signal_type, market_id)
        rows = conn.execute("""
            SELECT s.source, s.signal_type, s.market_id, s.description,
                   s.value, s.value_shift, s.composite_score, s.timestamp
            FROM alt_data_signals s
            INNER JOIN (
                SELECT source, signal_type, market_id, MAX(timestamp) AS max_ts
                FROM alt_data_signals
                GROUP BY source, signal_type, market_id
            ) latest
              ON s.source    = latest.source
             AND s.signal_type = latest.signal_type
             AND (s.market_id IS latest.market_id OR
                  (s.market_id IS NULL AND latest.market_id IS NULL))
             AND s.timestamp = latest.max_ts
            ORDER BY s.timestamp DESC
        """).fetchall()

        # Build lookup: (source, signal_type, market_id) -> value
        def v(src, st, mid=""):
            for r in rows:
                if r["source"]==src and r["signal_type"]==st and (r["market_id"] or "")==mid:
                    return r["value"]
            return None

        def ts(src):
            for r in rows:
                if r["source"]==src:
                    return r["timestamp"]
            return None

        # Latest composite score
        comp = v("composite", "score")
        comp_row = next((r for r in rows if r["source"]=="composite"), None)

        # Fear & Greed label
        fg = v("fear_greed", "index")
        fg_int = int(fg) if fg is not None else None
        if fg_int is None:
            fg_label = None
        elif fg_int <= 25:
            fg_label = "Extreme Fear"
        elif fg_int <= 45:
            fg_label = "Fear"
        elif fg_int <= 55:
            fg_label = "Neutral"
        elif fg_int <= 75:
            fg_label = "Greed"
        else:
            fg_label = "Extreme Greed"

        # Composite components: reconstruct from last poll cycle
        # We approximate by pulling all rows from the same minute as composite
        components = {}
        if comp_row:
            comp_ts_prefix = comp_row["timestamp"][:16]  # "YYYY-MM-DD HH:MM"
            cycle_rows = conn.execute("""
                SELECT source, signal_type, market_id, value
                FROM alt_data_signals
                WHERE timestamp LIKE ?
                ORDER BY timestamp DESC
            """, (comp_ts_prefix + "%",)).fetchall()

            fg_v = next((r["value"] for r in cycle_rows if r["source"]=="fear_greed"), None)
            if fg_v is not None:
                components["Fear & Greed"] = round((fg_v - 50) * 2, 1)

            cg_v = next((r["value"] for r in cycle_rows if r["source"]=="coingecko"), None)
            if cg_v is not None:
                components["BTC Sentiment"] = round(cg_v * 100, 1)

            sol_tvl_rows = [r for r in cycle_rows if r["source"]=="defillama" and r["market_id"]=="solana"]
            # TVL trend needs current + 7d — approximate from value_shift stored
            # Use funding rate as proxy component
            btc_fr = next((r["value"] for r in cycle_rows if r["source"]=="coinalyze" and r["signal_type"]=="funding" and "BTC" in (r["market_id"] or "")), None)
            eth_fr = next((r["value"] for r in cycle_rows if r["source"]=="coinalyze" and r["signal_type"]=="funding" and "ETH" in (r["market_id"] or "")), None)
            if btc_fr is not None and eth_fr is not None:
                avg_fr = (btc_fr + eth_fr) / 2
                components["Funding Rate"] = round(max(-100.0, min(100.0, -(avg_fr / 0.0003) * 100.0)), 1)

            kx_btc = next((r["value"] for r in cycle_rows if r["source"]=="kalshi" and r["market_id"]=="KXBTC"), None)
            kx_eth = next((r["value"] for r in cycle_rows if r["source"]=="kalshi" and r["market_id"]=="KXETH"), None)
            kalshi_vals = [x for x in [kx_btc, kx_eth] if x is not None]
            if kalshi_vals:
                avg_prob = sum(kalshi_vals) / len(kalshi_vals)
                components["Kalshi crypto avg"] = round((avg_prob - 0.5) * 200, 1)

        # TVL 7d change — stored as value_shift on defillama rows
        sol_tvl_row = next((r for r in rows if r["source"]=="defillama" and r["market_id"]=="solana"), None)
        base_tvl_row = next((r for r in rows if r["source"]=="defillama" and r["market_id"]=="base"), None)
        sol_tvl_now = sol_tvl_row["value"] if sol_tvl_row else None
        sol_tvl_7d = (sol_tvl_now - sol_tvl_row["value_shift"]) if (sol_tvl_row and sol_tvl_row["value_shift"] is not None) else None
        base_tvl_now = base_tvl_row["value"] if base_tvl_row else None

        # Last updated = most recent timestamp in table
        last_ts_row = conn.execute("SELECT MAX(timestamp) as t FROM alt_data_signals").fetchone()
        last_updated = last_ts_row["t"] if last_ts_row else None

        return JSONResponse({
            "composite":             round(comp, 1) if comp is not None else None,
            "composite_components":  components,
            "fear_greed":            fg_int,
            "fg_label":              fg_label,
            "coingecko_sentiment":   v("coingecko", "sentiment"),
            "kalshi_btc_prob":       v("kalshi", "prediction", "KXBTC"),
            "kalshi_eth_prob":       v("kalshi", "prediction", "KXETH"),
            "kalshi_sol_prob":       v("kalshi", "prediction", "KXSOL"),
            "recession_prob":        v("kalshi", "prediction", "KXRECESSION"),
            "coinalyze_btc_funding": v("coinalyze", "funding", "BTCUSDT_PERP.A"),
            "coinalyze_eth_funding": v("coinalyze", "funding", "ETHUSDT_PERP.A"),
            "coinalyze_sol_funding": v("coinalyze", "funding", "SOLUSDT_PERP.A"),
            "coinalyze_btc_oi":      v("coinalyze", "open_interest", "BTCUSDT_PERP.A"),
            "coinalyze_eth_oi":      v("coinalyze", "open_interest", "ETHUSDT_PERP.A"),
            "solana_tvl_now":        sol_tvl_now,
            "solana_tvl_7d_ago":     sol_tvl_7d,
            "base_tvl_now":          base_tvl_now,
            "helius_sol_inflation":  v("helius", "macro", "sol_inflation"),
            "helius_sol_epoch":      None,
            "last_updated":          last_updated,
            "last_error":            None,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)})
    finally:
        conn.close()

# ── TASK 9: Ensure flash_loans table on startup ──────────────────────────────

@app.on_event("startup")
def create_flash_loans_table():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flash_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL,
                profit_usdc REAL DEFAULT 0,
                gas_cost_eth REAL DEFAULT 0,
                block_number INTEGER,
                executed_at TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass  # Non-fatal — DB may be locked at startup


# ── TASK 1: GET /api/prices — Live Position Prices via DEXScreener ───────────

@app.get("/api/prices")
async def api_prices():
    global _price_cache
    now = time.time()
    if now - _price_cache["ts"] < 60 and _price_cache["data"]:
        return JSONResponse({"prices": _price_cache["data"]})

    conn = get_db()
    if not conn:
        return JSONResponse({"prices": {}})
    try:
        rows = conn.execute(
            "SELECT DISTINCT token_addr FROM positions WHERE status='open' AND token_addr IS NOT NULL AND token_addr != ''"
        ).fetchall()
        addrs = [r["token_addr"] for r in rows]
    finally:
        conn.close()

    if not addrs:
        return JSONResponse({"prices": {}})

    prices = {}
    async with httpx.AsyncClient(timeout=10) as client:
        tasks = [_fetch_dex_price(client, addr) for addr in addrs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for addr, result in zip(addrs, results):
            if isinstance(result, Exception) or result is None:
                prices[addr] = None
            else:
                prices[addr] = result

    _price_cache = {"data": prices, "ts": now}
    return JSONResponse({"prices": prices})


async def _fetch_dex_price(client, token_addr):
    try:
        resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}")
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        pair = pairs[0]
        return {
            "price_usd": float(pair.get("priceUsd") or 0),
            "price_change_24h": float(pair.get("priceChange", {}).get("h24") or 0),
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
    except Exception:
        return None


# ── TASK 2: GET /api/balances — On-Chain Wallet Balances ─────────────────────

@app.get("/api/balances")
async def api_balances():
    global _balance_cache
    now = time.time()
    if now - _balance_cache["ts"] < 30 and _balance_cache["data"]:
        return JSONResponse(_balance_cache["data"])

    import asyncio
    base_data = await asyncio.to_thread(_fetch_base_balances)
    sol_data = await _fetch_solana_balances()

    total_usdc = base_data.get("usdc", 0) + sol_data.get("usdc", 0)
    result = {
        "base": base_data,
        "solana": sol_data,
        "total_usdc": round(total_usdc, 2),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _balance_cache = {"data": result, "ts": now}
    return JSONResponse(result)


def _fetch_base_balances():
    result = {"eth": 0, "usdc": 0, "flash_loan_contract_eth": 0}
    try:
        from web3 import Web3
        rpc_url = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        # Native ETH balance
        eth_wei = w3.eth.get_balance(Web3.to_checksum_address(BASE_WALLET))
        result["eth"] = round(eth_wei / 1e18, 6)

        # USDC balance (6 decimals)
        usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(BASE_USDC_CONTRACT),
            abi=ERC20_BALANCE_ABI,
        )
        usdc_raw = usdc_contract.functions.balanceOf(Web3.to_checksum_address(BASE_WALLET)).call()
        result["usdc"] = round(usdc_raw / 1e6, 2)

        # Flash loan contract ETH
        fl_wei = w3.eth.get_balance(Web3.to_checksum_address(FLASH_LOAN_CONTRACT))
        result["flash_loan_contract_eth"] = round(fl_wei / 1e18, 6)
    except Exception as e:
        result["error"] = str(e)
    return result


async def _fetch_solana_balances():
    result = {"sol": 0, "usdc": 0}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # SOL balance
            resp = await client.post(
                "https://api.mainnet-beta.solana.com",
                json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [SOLANA_WALLET]},
            )
            data = resp.json()
            lamports = data.get("result", {}).get("value", 0)
            result["sol"] = round(lamports / 1e9, 6)

            # USDC token account
            resp2 = await client.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc": "2.0", "id": 2, "method": "getTokenAccountsByOwner",
                    "params": [
                        SOLANA_WALLET,
                        {"mint": SOLANA_USDC_MINT},
                        {"encoding": "jsonParsed"},
                    ],
                },
            )
            data2 = resp2.json()
            accounts = data2.get("result", {}).get("value", [])
            if accounts:
                info = accounts[0].get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                amount = info.get("tokenAmount", {}).get("uiAmount", 0)
                result["usdc"] = round(float(amount or 0), 2)
    except Exception as e:
        result["error"] = str(e)
    return result


# ── TASK 3: GET /api/flashloans — Flash Loan On-Chain Data ───────────────────

@app.get("/api/flashloans")
async def api_flashloans():
    # Fetch from BaseScan and upsert into DB
    await _sync_flashloan_txs()

    conn = get_db()
    if not conn:
        return JSONResponse({"summary": {}, "recent": []})
    try:
        rows = conn.execute("SELECT * FROM flash_loans ORDER BY executed_at DESC").fetchall()
        all_txs = rows_to_dicts(rows)

        total = len(all_txs)
        successful = sum(1 for t in all_txs if t["status"] == "success")
        reverted = sum(1 for t in all_txs if t["status"] == "reverted")
        total_profit = sum(t.get("profit_usdc", 0) or 0 for t in all_txs)
        total_gas = sum(t.get("gas_cost_eth", 0) or 0 for t in all_txs)

        return JSONResponse({
            "summary": {
                "total_attempts": total,
                "successful": successful,
                "reverted": reverted,
                "total_profit_usdc": round(total_profit, 2),
                "total_gas_eth": round(total_gas, 6),
                "success_rate": round(successful / total * 100, 1) if total else 0,
            },
            "recent": all_txs[:20],
        })
    finally:
        conn.close()


async def _sync_flashloan_txs():
    """
    Pull transactions from BaseScan and upsert into flash_loans table.
    Profit is calculated from USDC token transfers TO the owner wallet,
    not from the ETH value field (which is always 0 for flash loans).
    Falls back gracefully if no API key or network is unavailable.
    """
    api_key = os.environ.get("BASESCAN_API_KEY", "YourApiKeyToken")
    base    = "https://api.basescan.org/api"

    tx_url  = (f"{base}?module=account&action=txlist"
               f"&address={FLASH_LOAN_CONTRACT}&sort=desc&apikey={api_key}")
    tok_url = (f"{base}?module=account&action=tokentx"
               f"&contractaddress={BASE_USDC_CONTRACT}"
               f"&address={FLASH_LOAN_CONTRACT}&sort=desc&apikey={api_key}")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            tx_resp, tok_resp = await asyncio.gather(
                client.get(tx_url),
                client.get(tok_url),
            )
        txs       = tx_resp.json().get("result", [])
        token_txs = tok_resp.json().get("result", [])
        if not isinstance(txs, list):
            return
    except Exception:
        return  # network down or rate limited — seeded rows remain untouched

    # Build profit map: tx_hash -> net USDC flowing to the owner wallet
    # (from=flash_loan_contract, to=owner_wallet means profit was taken)
    profit_by_hash: dict = {}
    if isinstance(token_txs, list):
        owner = BASE_WALLET.lower()
        fl    = FLASH_LOAN_CONTRACT.lower()
        for ttx in token_txs:
            h   = ttx.get("hash", "")
            frm = ttx.get("from", "").lower()
            to  = ttx.get("to", "").lower()
            try:
                amt = int(ttx.get("value", 0)) / 1e6  # USDC has 6 decimals
            except (ValueError, TypeError):
                amt = 0.0
            if h not in profit_by_hash:
                profit_by_hash[h] = 0.0
            if frm == fl and to == owner:
                profit_by_hash[h] += amt

    conn = sqlite3.connect(str(DB_PATH), timeout=2)
    conn.row_factory = sqlite3.Row
    for tx in txs[:100]:
        tx_hash = tx.get("hash", "")
        if not tx_hash:
            continue
        is_error     = tx.get("isError", "0")
        status       = "success" if is_error == "0" else "reverted"
        gas_used     = int(tx.get("gasUsed", 0) or 0)
        gas_price    = int(tx.get("gasPrice", 0) or 0)
        gas_cost_eth = gas_used * gas_price / 1e18
        profit_usdc  = profit_by_hash.get(tx_hash, 0.0) if status == "success" else 0.0
        block        = int(tx.get("blockNumber", 0) or 0)
        ts           = int(tx.get("timeStamp", 0) or 0)
        executed_at  = (datetime.fromtimestamp(ts, tz=timezone.utc)
                        .strftime("%Y-%m-%d %H:%M:%S") if ts else None)

        conn.execute("""
            INSERT OR IGNORE INTO flash_loans
                (tx_hash, status, profit_usdc, gas_cost_eth, block_number, executed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (tx_hash, status, round(profit_usdc, 4),
              round(gas_cost_eth, 8), block, executed_at))
    conn.commit()
    conn.close()


# ── TASK 4: GET /api/export/trades-csv — Tax Export ──────────────────────────

@app.get("/api/export/trades-csv")
def api_export_trades_csv():
    conn = get_db()
    if not conn:
        return Response("No data", media_type="text/plain", status_code=404)
    try:
        rows = conn.execute("""
            SELECT t.executed_at, p.symbol, p.chain, t.exchange, t.action,
                   t.quantity, t.price_usd, t.usd_value, p.cost_basis_usd, t.pnl_usd,
                   t.fee_usd, t.tx_hash
            FROM trades t
            LEFT JOIN positions p ON t.position_id = p.id
            WHERE t.action IN ('sell', 'close', 'stop_loss', 'take_profit')
            ORDER BY t.executed_at ASC
        """).fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["date", "token", "chain", "exchange", "action", "quantity",
                         "price_usd", "proceeds_usd", "cost_basis_usd", "pnl_usd", "fee_usd", "tx_hash"])
        for r in rows:
            date_str = r["executed_at"][:10] if r["executed_at"] else ""
            writer.writerow([
                date_str, r["symbol"] or "", r["chain"] or "", r["exchange"] or "",
                (r["action"] or "").upper(), r["quantity"] or 0,
                r["price_usd"] or 0, r["usd_value"] or 0, r["cost_basis_usd"] or 0,
                r["pnl_usd"] or 0, r["fee_usd"] or 0, r["tx_hash"] or "",
            ])

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"breadbot_trades_{today}.csv"
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    finally:
        conn.close()


# ── TASK 5: GET /api/analytics — Performance Analytics ───────────────────────

@app.get("/api/analytics")
def api_analytics():
    conn = get_db()
    if not conn:
        return JSONResponse({})
    try:
        # Overall win rate
        total = conn.execute("SELECT COUNT(*) as c FROM trades WHERE pnl_usd IS NOT NULL").fetchone()["c"]
        wins = conn.execute("SELECT COUNT(*) as c FROM trades WHERE pnl_usd > 0").fetchone()["c"]
        win_rate = round(wins / total * 100, 1) if total else 0

        # Win rate by chain
        chain_rows = conn.execute("""
            SELECT p.chain, COUNT(*) as total,
                   SUM(CASE WHEN t.pnl_usd > 0 THEN 1 ELSE 0 END) as wins
            FROM trades t LEFT JOIN positions p ON t.position_id = p.id
            WHERE t.pnl_usd IS NOT NULL AND p.chain IS NOT NULL
            GROUP BY p.chain
        """).fetchall()
        win_by_chain = {}
        for r in chain_rows:
            win_by_chain[r["chain"]] = round(r["wins"] / r["total"] * 100, 1) if r["total"] else 0

        # Win rate by score band
        score_rows = conn.execute("""
            SELECT
                CASE
                    WHEN a.rug_score >= 80 THEN '80-100'
                    WHEN a.rug_score >= 60 THEN '60-79'
                    ELSE 'below-60'
                END as band,
                COUNT(*) as total,
                SUM(CASE WHEN t.pnl_usd > 0 THEN 1 ELSE 0 END) as wins
            FROM trades t
            LEFT JOIN positions p ON t.position_id = p.id
            LEFT JOIN meme_alerts a ON p.token_addr = a.token_addr
            WHERE t.pnl_usd IS NOT NULL AND a.rug_score IS NOT NULL
            GROUP BY band
        """).fetchall()
        win_by_score = {}
        for r in score_rows:
            win_by_score[r["band"]] = round(r["wins"] / r["total"] * 100, 1) if r["total"] else 0

        # Avg hold time (hours)
        hold_rows = conn.execute("""
            SELECT AVG(
                (julianday(closed_at) - julianday(opened_at)) * 24
            ) as avg_hours
            FROM positions WHERE status != 'open' AND closed_at IS NOT NULL AND opened_at IS NOT NULL
        """).fetchone()
        avg_hold = round(hold_rows["avg_hours"], 1) if hold_rows["avg_hours"] else 0

        # Best/worst trade
        best = conn.execute("""
            SELECT p.symbol, t.pnl_usd,
                   CASE WHEN t.usd_value > 0 THEN (t.pnl_usd / t.usd_value * 100) ELSE 0 END as pct_gain
            FROM trades t LEFT JOIN positions p ON t.position_id = p.id
            WHERE t.pnl_usd IS NOT NULL ORDER BY t.pnl_usd DESC LIMIT 1
        """).fetchone()
        worst = conn.execute("""
            SELECT p.symbol, t.pnl_usd,
                   CASE WHEN t.usd_value > 0 THEN (t.pnl_usd / t.usd_value * 100) ELSE 0 END as pct_gain
            FROM trades t LEFT JOIN positions p ON t.position_id = p.id
            WHERE t.pnl_usd IS NOT NULL ORDER BY t.pnl_usd ASC LIMIT 1
        """).fetchone()

        best_trade = {"symbol": best["symbol"] or "?", "pnl_usd": round(best["pnl_usd"], 2), "pct_gain": round(best["pct_gain"], 1)} if best and best["pnl_usd"] else None
        worst_trade = {"symbol": worst["symbol"] or "?", "pnl_usd": round(worst["pnl_usd"], 2), "pct_gain": round(worst["pct_gain"], 1)} if worst and worst["pnl_usd"] else None

        # Max drawdown
        pnl_series = conn.execute("""
            SELECT pnl_usd FROM trades WHERE pnl_usd IS NOT NULL ORDER BY executed_at ASC
        """).fetchall()
        cumulative = 0
        peak = 0
        max_dd = 0
        for r in pnl_series:
            cumulative += r["pnl_usd"]
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Total fees
        fees = conn.execute("SELECT COALESCE(SUM(fee_usd), 0) as s FROM trades").fetchone()["s"]

        # Avg position size
        avg_pos = conn.execute("SELECT AVG(cost_basis_usd) as a FROM positions WHERE cost_basis_usd > 0").fetchone()["a"]

        # Monthly PnL
        monthly_rows = conn.execute("""
            SELECT strftime('%Y-%m', executed_at) as month, SUM(COALESCE(pnl_usd, 0)) as pnl
            FROM trades GROUP BY month ORDER BY month ASC
        """).fetchall()
        monthly_pnl = [{"month": r["month"], "pnl": round(r["pnl"], 2)} for r in monthly_rows]

        return JSONResponse({
            "win_rate_overall": win_rate,
            "win_rate_by_chain": win_by_chain,
            "win_rate_by_score_band": win_by_score,
            "avg_hold_time_hours": avg_hold,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "max_drawdown_usd": round(max_dd, 2),
            "total_fees_usd": round(fees, 2),
            "avg_position_size_usd": round(avg_pos, 2) if avg_pos else 0,
            "monthly_pnl": monthly_pnl,
        })
    finally:
        conn.close()


# ── TASK 6: Risk Config Endpoints ────────────────────────────────────────────

RISK_DEFAULTS = {
    "max_position_size_pct": 0.02,
    "daily_loss_limit_pct": 0.05,
    "min_liquidity_usd": 15000,
    "min_volume_24h_usd": 40000,
    "min_rug_score": 50,
    "portfolio_total_usd": 5000,
}

RISK_RANGES = {
    "max_position_size_pct": (0.001, 0.10),
    "daily_loss_limit_pct": (0.01, 0.20),
    "min_liquidity_usd": (1000, 1000000),
    "min_volume_24h_usd": (1000, 10000000),
    "min_rug_score": (0, 100),
    "portfolio_total_usd": (100, 1000000),
}


@app.get("/api/risk-config")
def api_risk_config_get():
    conn = get_db()
    result = dict(RISK_DEFAULTS)
    if conn:
        try:
            rows = conn.execute("SELECT key, value FROM bot_config").fetchall()
            cfg = {r["key"]: r["value"] for r in rows}
            for k in RISK_DEFAULTS:
                if k in cfg:
                    try:
                        result[k] = float(cfg[k])
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
        finally:
            conn.close()
    return JSONResponse(result)


class RiskConfigBody(BaseModel):
    max_position_size_pct: Optional[float] = None
    daily_loss_limit_pct: Optional[float] = None
    min_liquidity_usd: Optional[float] = None
    min_volume_24h_usd: Optional[float] = None
    min_rug_score: Optional[float] = None
    portfolio_total_usd: Optional[float] = None


@app.post("/api/risk-config")
def api_risk_config_post(body: RiskConfigBody):
    updates = body.model_dump(exclude_none=True)
    errors = []
    for k, v in updates.items():
        lo, hi = RISK_RANGES.get(k, (None, None))
        if lo is not None and (v < lo or v > hi):
            errors.append(f"{k} must be between {lo} and {hi}")
    if errors:
        raise HTTPException(400, detail="; ".join(errors))

    conn = get_db_rw()
    try:
        for k, v in updates.items():
            conn.execute("""
                INSERT OR REPLACE INTO bot_config (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
            """, (k, str(v)))
        conn.commit()

        # Also write to .env file
        env_path = Path(__file__).parent.parent / ".env"
        _update_env_file(env_path, updates)

        return JSONResponse({"ok": True, "updated": list(updates.keys())})
    finally:
        conn.close()


def _update_env_file(env_path, updates):
    """Write/update keys in a .env file."""
    lines = []
    existing_keys = set()
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    new_lines = []
    for line in lines:
        key_part = line.split("=", 1)[0].strip() if "=" in line else ""
        upper_key = key_part.upper()
        matched = False
        for k, v in updates.items():
            if k.upper() == upper_key:
                new_lines.append(f"{k.upper()}={v}")
                existing_keys.add(k)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in existing_keys:
            new_lines.append(f"{k.upper()}={v}")

    env_path.write_text("\n".join(new_lines) + "\n")


# ── TASK 7: GET /api/research/{token_addr} — Token Research Tool ─────────────

@app.get("/api/research/{token_addr}")
async def api_research(token_addr: str):
    # Detect chain by address format
    chain = "solana" if not token_addr.startswith("0x") else "base"
    chain_id = "solana" if chain == "solana" else "8453"

    result = {
        "token_addr": token_addr,
        "chain": chain,
        "rug_score": 100,
        "flags": [],
        "goplus": {},
        "rugcheck": {},
        "dexscreener": {},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        # GoPlus
        try:
            gp_resp = await client.get(
                f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}",
                params={"contract_addresses": token_addr},
            )
            gp_data = gp_resp.json()
            gp_result = gp_data.get("result", {})
            gp = gp_result.get(token_addr.lower()) or gp_result.get(token_addr) or {}
            result["goplus"] = {
                "is_honeypot": str(gp.get("is_honeypot", "0")) == "1",
                "sell_tax": float(gp.get("sell_tax", 0) or 0),
                "buy_tax": float(gp.get("buy_tax", 0) or 0),
                "owner_address": gp.get("owner_address", ""),
            }
            # Score deductions
            flags = []
            score = 100
            if result["goplus"]["is_honeypot"]:
                flags.append("Honeypot detected"); score -= 40
            if str(gp.get("is_mintable", "0")) == "1":
                flags.append("Mintable supply"); score -= 15
            if str(gp.get("is_blacklisted", "0")) == "1":
                flags.append("Has blacklist"); score -= 20
            if str(gp.get("transfer_pausable", "0")) == "1":
                flags.append("Transfer pausable"); score -= 20
            owner = gp.get("owner_address", "")
            if owner and owner != "0x0000000000000000000000000000000000000000":
                flags.append("Owner not renounced"); score -= 10
            if result["goplus"]["sell_tax"] > 5:
                flags.append(f"High sell tax ({result['goplus']['sell_tax']}%)"); score -= 15
            if result["goplus"]["buy_tax"] > 5:
                flags.append(f"High buy tax ({result['goplus']['buy_tax']}%)"); score -= 10
            result["flags"] = flags
            result["rug_score"] = max(0, score)
        except Exception:
            pass

        # RugCheck
        try:
            rc_resp = await client.get(f"https://api.rugcheck.xyz/v1/tokens/{token_addr}/report")
            if rc_resp.status_code == 200:
                rc_data = rc_resp.json()
                risks = rc_data.get("risks", [])
                result["rugcheck"] = {
                    "score": rc_data.get("score", 0),
                    "risks": [r.get("name", "") for r in risks],
                }
                if any(r.get("level") == "critical" for r in risks):
                    result["flags"].append("RugCheck critical risk")
                    result["rug_score"] = max(0, result["rug_score"] - 15)
        except Exception:
            pass

        # DEXScreener
        try:
            dx_resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}")
            dx_data = dx_resp.json()
            pairs = dx_data.get("pairs") or []
            if pairs:
                p = pairs[0]
                result["dexscreener"] = {
                    "name": p.get("baseToken", {}).get("name", ""),
                    "symbol": p.get("baseToken", {}).get("symbol", ""),
                    "price_usd": float(p.get("priceUsd") or 0),
                    "liquidity": float(p.get("liquidity", {}).get("usd") or 0),
                    "volume_24h": float(p.get("volume", {}).get("h24") or 0),
                }
        except Exception:
            pass

    return JSONResponse(result)


# ── Static files + SPA fallback ───────────────────────────────────────────────

# ── Execution Config: GET ─────────────────────────────────────────────────────

STRATEGIES_META = {
    "conservative": {"min_score": 85, "max_market_cap": 1_000_000, "position_multiplier": 0.5,
                     "label": "Conservative", "desc": "Score 85+, mkt cap under $1M, half position size"},
    "balanced":     {"min_score": 78, "max_market_cap": 2_000_000, "position_multiplier": 1.0,
                     "label": "Balanced",     "desc": "Score 78+, mkt cap under $2M, full position size"},
    "aggressive":   {"min_score": 68, "max_market_cap": 5_000_000, "position_multiplier": 1.5,
                     "label": "Aggressive",   "desc": "Score 68+, mkt cap under $5M, 1.5x position size"},
}

@app.get("/api/execution-config")
def api_execution_config_get():
    conn = get_db()
    cfg = {}
    auto_trades_today = 0
    if conn:
        try:
            rows = conn.execute(
                "SELECT key, value FROM bot_config WHERE key IN "
                "('execution_mode','auto_strategy','auto_max_trades_day')"
            ).fetchall()
            cfg = {r["key"]: r["value"] for r in rows}
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            row = conn.execute(
                "SELECT COUNT(*) as c FROM meme_alerts "
                "WHERE decision='auto_buy' AND date(created_at)=?", (today,)
            ).fetchone()
            auto_trades_today = row["c"] if row else 0
        except Exception:
            pass
        finally:
            conn.close()
    return JSONResponse({
        "execution_mode":      cfg.get("execution_mode", "manual"),
        "auto_strategy":       cfg.get("auto_strategy", "balanced"),
        "auto_max_trades_day": int(cfg.get("auto_max_trades_day", 5) or 5),
        "auto_trades_today":   auto_trades_today,
        "strategies":          STRATEGIES_META,
    })


class ExecutionConfigBody(BaseModel):
    execution_mode: str
    auto_strategy: str = "balanced"
    auto_max_trades_day: int = 5


@app.post("/api/execution-config")
def api_execution_config_post(body: ExecutionConfigBody):
    if body.execution_mode not in ("manual", "auto"):
        raise HTTPException(400, "execution_mode must be 'manual' or 'auto'")
    if body.auto_strategy not in STRATEGIES_META:
        raise HTTPException(400, "auto_strategy must be conservative, balanced, or aggressive")
    if not (1 <= body.auto_max_trades_day <= 50):
        raise HTTPException(400, "auto_max_trades_day must be 1-50")
    conn = get_db_rw()
    try:
        now = datetime.now(timezone.utc).isoformat()
        for key, val in [
            ("execution_mode",      body.execution_mode),
            ("auto_strategy",       body.auto_strategy),
            ("auto_max_trades_day", str(body.auto_max_trades_day)),
        ]:
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?,?,?)",
                (key, val, now)
            )
        conn.commit()
        return JSONResponse({"ok": True, "execution_mode": body.execution_mode,
                             "auto_strategy": body.auto_strategy})
    finally:
        conn.close()



# ══ PANEL-COMPATIBLE ROUTES (auto-generated) ══════════════════════════

@app.get("/api/auth/status")
def auth_status_stub():
    return {"configured": True}

@app.get("/api/auth/me")
def auth_me_stub():
    return {"authenticated": True}

@app.post("/api/auth/login")
def auth_login_stub():
    return {"ok": True}

@app.post("/api/auth/logout")
def auth_logout_stub():
    return {"ok": True}

@app.post("/api/auth/setup")
def auth_setup_stub():
    return {"ok": True}

@app.get("/api/bot/status")
async def bot_status():
    db = get_db()
    if not db:
        return {"trading_active": False, "open_positions": 0, "total_pnl": 0}
    try:
        c = db.cursor()
        paused = False
        try:
            row = c.execute("SELECT value FROM bot_config WHERE key='trading_paused'").fetchone()
            if row: paused = str(row["value"]) == "1"
        except Exception: pass
        positions = c.execute("SELECT COUNT(*) as cnt FROM positions WHERE status='open'").fetchone()
        open_pos = positions["cnt"] if positions else 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl_row = c.execute("SELECT COALESCE(SUM(pnl_usd),0) as pnl FROM trades WHERE DATE(executed_at)=? AND pnl_usd IS NOT NULL", (today,)).fetchone()
        total_pnl = round(pnl_row["pnl"], 2) if pnl_row else 0
        exec_mode = "manual"
        try:
            em = c.execute("SELECT value FROM bot_config WHERE key='execution_mode'").fetchone()
            if em: exec_mode = em["value"]
        except Exception: pass
        return {"trading_active": not paused, "trading_paused": paused, "open_positions": open_pos,
                "total_pnl": total_pnl, "today_realized_pnl": total_pnl,
                "max_position_size_pct": float(os.environ.get("MAX_POSITION_SIZE_PCT","0.02")),
                "max_positions": int(os.environ.get("MAX_OPEN_POSITIONS","5")),
                "execution_mode": exec_mode,
                "daily_loss_limit_pct": float(os.environ.get("DAILY_LOSS_LIMIT_PCT","0.05"))}
    finally: db.close()

@app.get("/api/bot/positions")
async def bot_positions():
    db = get_db()
    if not db: return {"positions": []}
    try:
        rows = db.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
        return {"positions": rows_to_dicts(rows)}
    finally: db.close()

@app.get("/api/bot/yields")
async def bot_yields():
    db = get_db()
    if not db: return {"platforms": [], "rebalance_threshold": 1.5}
    try:
        rows = db.execute("""SELECT ys.* FROM yield_snapshots ys
            INNER JOIN (SELECT platform, asset, MAX(recorded_at) as max_ts FROM yield_snapshots GROUP BY platform, asset) latest
            ON ys.platform=latest.platform AND ys.asset=latest.asset AND ys.recorded_at=latest.max_ts ORDER BY ys.apy DESC""").fetchall()
        platforms = []
        for r in rows:
            d = dict(r)
            platforms.append({
                "platform": d.get("platform", ""),
                "apy": d.get("apy", 0),
                "type": d.get("asset", "USDC"),
                "current": False,
            })
        last_ts = None
        if platforms:
            try:
                ts_row = db.execute("SELECT MAX(recorded_at) as ts FROM yield_snapshots").fetchone()
                if ts_row and ts_row["ts"]:
                    from datetime import datetime as _dt2
                    last_ts = int(_dt2.fromisoformat(ts_row["ts"]).timestamp())
            except Exception:
                pass
        return {"platforms": platforms, "rebalance_threshold": float(os.environ.get("REBALANCE_THRESHOLD_PCT", "1.5")), "last_updated": last_ts}
    finally: db.close()

@app.get("/api/bot/alerts/history")
async def bot_alerts_history():
    db = get_db()
    if not db: return {"alerts": []}
    try:
        rows = db.execute("SELECT * FROM meme_alerts ORDER BY created_at DESC LIMIT 200").fetchall()
        alerts = []
        for r in rows:
            d = dict(r)
            # Parse created_at to unix timestamp
            ts = 0
            try:
                from datetime import datetime as _dt
                dt = _dt.fromisoformat(d.get("created_at", ""))
                ts = int(dt.timestamp())
            except Exception:
                ts = int(time.time())
            # Parse rug_flags into structured flags list
            flags = []
            raw_flags = d.get("rug_flags", "") or ""
            if raw_flags:
                import json as _jf
                try:
                    flag_list = _jf.loads(raw_flags) if raw_flags.startswith("[") else [s.strip() for s in raw_flags.split(",") if s.strip()]
                except Exception:
                    flag_list = [s.strip() for s in raw_flags.split(",") if s.strip()]
                for f in flag_list:
                    fl = f.lower()
                    ftype = "risk" if any(w in fl for w in ["honeypot","blacklist","pause","proxy","high tax","pumped"]) else "warn" if any(w in fl for w in ["mint","owner","concentrated","not locked","unlocked"]) else "ok"
                    flags.append({"label": f, "type": ftype})
            decided = d.get("decision", "pending") not in ("pending", "")
            alerts.append({
                "id": d.get("id"),
                "chain": d.get("chain", ""),
                "token": d.get("token_name", d.get("symbol", "")),
                "symbol": d.get("symbol", ""),
                "contract": d.get("token_addr", ""),
                "security_score": d.get("rug_score", 0),
                "price": d.get("price_usd"),
                "liquidity_usd": d.get("liquidity"),
                "volume_24h": d.get("volume_24h"),
                "market_cap": d.get("mcap"),
                "age_hours": None,
                "position_size_usd": None,
                "source": "Scanner",
                "timestamp": ts,
                "expires_at": ts + 900,
                "flags": flags,
                "actioned": decided,
                "action": d.get("decision") if decided else None,
            })
        return {"alerts": alerts}
    finally: db.close()

@app.get("/api/bot/pnl")
async def bot_pnl():
    db = get_db()
    if not db: return {"total_pnl": 0, "trade_count": 0}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = db.execute("SELECT COALESCE(SUM(pnl_usd),0) as pnl, COUNT(*) as cnt FROM trades WHERE DATE(executed_at)=? AND pnl_usd IS NOT NULL", (today,)).fetchone()
        return {"total_pnl": round(row["pnl"],2) if row else 0, "realized_pnl_usd": round(row["pnl"],2) if row else 0, "trade_count": row["cnt"] if row else 0}
    finally: db.close()

@app.get("/api/bot/strategy/performance")
async def bot_strategy_performance():
    db = get_db()
    if not db: return {"scanner":{"pnl":0,"trades":0},"grid":{"pnl":0,"cycles":0},"funding":{"pnl":0,"collected":0}}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        sr = db.execute("SELECT COALESCE(SUM(pnl_usd),0) as pnl, COUNT(*) as cnt FROM trades WHERE pnl_usd IS NOT NULL AND DATE(executed_at)>=?", (cutoff,)).fetchone()
        grid_pnl, grid_cycles, fund_collected = 0, 0, 0
        try:
            gr = db.execute("SELECT COALESCE(SUM(profit_usd),0) as pnl, COUNT(*) as cnt FROM grid_fills WHERE DATE(filled_at)>=?", (cutoff,)).fetchone()
            if gr: grid_pnl, grid_cycles = round(gr["pnl"],2), gr["cnt"]
        except: pass
        try:
            fr = db.execute("SELECT COALESCE(SUM(funding_collected_usd),0) as c FROM funding_positions WHERE DATE(opened_at)>=?", (cutoff,)).fetchone()
            if fr: fund_collected = round(fr["c"],2)
        except: pass
        return {"scanner":{"pnl":round(sr["pnl"],2) if sr else 0,"trades":sr["cnt"] if sr else 0},"grid":{"pnl":grid_pnl,"profit_usd":grid_pnl,"cycles":grid_cycles,"volume_usd":0},"funding_arb":{"pnl":fund_collected,"collected":fund_collected,"funding_collected_usd":fund_collected,"open_positions":0}}
    finally: db.close()

@app.get("/api/bot/grid/status")
async def bot_grid_status():
    db = get_db()
    if not db: return {"state":"STANDBY","pair":"","profit_usd":0,"completed_cycles":0}
    try:
        state = "STANDBY"
        try:
            row = db.execute("SELECT value FROM bot_config WHERE key='grid_state'").fetchone()
            if row: state = row["value"]
        except: pass
        profit, cycles = 0, 0
        try:
            gr = db.execute("SELECT COALESCE(SUM(profit_usd),0) as pnl, COUNT(*) as cnt FROM grid_fills").fetchone()
            if gr: profit, cycles = round(gr["pnl"],2), gr["cnt"]
        except: pass
        return {"state":state,"pair":os.environ.get("GRID_PAIR","BTC/USDT"),"profit_usd":profit,"completed_cycles":cycles,
                "upper_pct":float(os.environ.get("GRID_UPPER_PCT","10")),"lower_pct":float(os.environ.get("GRID_LOWER_PCT","10")),
                "num_levels":int(os.environ.get("GRID_NUM_LEVELS","20")),"rsi_guard":os.environ.get("GRID_RSI_GUARD","true").lower()=="true"}
    finally: db.close()

@app.get("/api/bot/funding/rates")
async def bot_funding_rates():
    db = get_db()
    arb_exchange = os.environ.get("FUNDING_ARB_EXCHANGE", "bybit")
    entry_t = float(os.environ.get("FUNDING_RATE_ENTRY_THRESHOLD", "0.01"))
    exit_t = float(os.environ.get("FUNDING_RATE_EXIT_THRESHOLD", "0.005"))
    arb_enabled = os.environ.get("FUNDING_ARB_ENABLED", "false").lower() in ("true", "1")
    venue_map = {
        "bybit": ("Bybit", "amber", None),
        "binance": ("Binance.US", "amber", True),
        "coinbase_cfm": ("Coinbase CFM", "green", True),
    }
    vl, vc, vlu = venue_map.get(arb_exchange, (arb_exchange, "amber", None))
    base = {
        "arb_exchange": arb_exchange,
        "venue_label": vl, "venue_color": vc, "venue_legal_us": vlu,
        "arb_enabled": arb_enabled,
        "entry_threshold_pct": entry_t, "exit_threshold_pct": exit_t,
        "rates": [],
    }
    if not db: return base
    try:
        rows = db.execute("""SELECT fr.* FROM funding_rate_history fr
            INNER JOIN (SELECT pair, MAX(recorded_at) as max_ts FROM funding_rate_history GROUP BY pair) latest
            ON fr.pair=latest.pair AND fr.recorded_at=latest.max_ts ORDER BY fr.pair""").fetchall()
        for r in rows:
            d = dict(r)
            rate = d.get("rate_8h", d.get("rate", 0)) or 0
            base["rates"].append({
                "pair": (d.get("pair", "") or "").replace("USDT", "").replace("/", ""),
                "rate_8h_pct": rate,
                "annualized_pct": rate * 3 * 365,
                "above_entry": abs(rate) >= entry_t,
            })
        return base
    except: return base
    finally: db.close()

@app.get("/api/bot/funding/positions")
async def bot_funding_positions():
    db = get_db()
    if not db: return {"positions": []}
    try:
        rows = db.execute("SELECT * FROM funding_positions WHERE closed_at IS NULL ORDER BY opened_at DESC").fetchall()
        return {"positions": rows_to_dicts(rows)}
    except: return {"positions": []}
    finally: db.close()

@app.get("/api/bot/channels")
async def bot_channels():
    db = get_db()
    if not db: return {"channels":[]}
    try:
        rows = db.execute("SELECT * FROM alpha_channels ORDER BY label").fetchall()
        return {"channels": rows_to_dicts(rows)}
    except: return {"channels":[]}
    finally: db.close()

@app.get("/api/bot/channels/hits")
async def bot_channels_hits():
    db = get_db()
    if not db: return {"hits":[]}
    try:
        rows = db.execute("SELECT * FROM alpha_channel_hits ORDER BY detected_at DESC LIMIT 50").fetchall()
        return {"hits": rows_to_dicts(rows)}
    except: return {"hits":[]}
    finally: db.close()

@app.get("/api/bot/backtest/results")
async def bot_backtest_results():
    import json as _json
    p = Path(__file__).parent.parent / "data" / "backtest_last.json"
    if p.exists():
        try:
            with open(p) as f: return _json.load(f)
        except: pass
    return {"status":"no_results"}

@app.get("/api/bot/pnl/history")
async def bot_pnl_history(days: int = 30):
    db = get_db()
    if not db: return []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = db.execute("SELECT DATE(executed_at) as date, COALESCE(SUM(pnl_usd),0) as pnl, COUNT(*) as trades FROM trades WHERE pnl_usd IS NOT NULL AND DATE(executed_at)>=? GROUP BY DATE(executed_at) ORDER BY DATE(executed_at)", (cutoff,)).fetchall()
        result = []
        cumulative = 0
        for r in rows:
            d = dict(r)
            net = d.get("pnl", 0) or 0
            cumulative += net
            result.append({"date": d.get("date"), "pnl": net, "net": net, "realized_pnl": net, "trades": d.get("trades", 0), "yield_earned": 0, "fees_paid": 0, "cumulative": round(cumulative, 4)})
        return result
    finally: db.close()

@app.get("/api/settings/basic")
async def settings_basic():
    return {"MAX_POSITION_SIZE_PCT":os.environ.get("MAX_POSITION_SIZE_PCT","0.02"),
            "DAILY_LOSS_LIMIT_PCT":os.environ.get("DAILY_LOSS_LIMIT_PCT","0.05"),
            "MIN_LIQUIDITY_USD":os.environ.get("MIN_LIQUIDITY_USD","15000"),
            "MIN_VOLUME_24H_USD":os.environ.get("MIN_VOLUME_24H_USD","40000"),
            "AUTO_EXECUTE_MIN_SCORE":os.environ.get("AUTO_EXECUTE_MIN_SCORE","83"),
            "ALERT_CHANNEL":os.environ.get("ALERT_CHANNEL","solana"),
            "AUTO_EXECUTE":os.environ.get("AUTO_EXECUTE","auto")}

@app.get("/api/settings/advanced")
async def settings_advanced():
    keys = ["COINBASE_API_KEY","COINBASE_SECRET_KEY","KRAKEN_API_KEY","KRAKEN_SECRET_KEY",
            "BYBIT_API_KEY","BYBIT_SECRET_KEY","BINANCE_API_KEY","BINANCE_SECRET_KEY",
            "GEMINI_API_KEY","GEMINI_SECRET_KEY","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID",
            "SOLANA_RPC_URL","EVM_BASE_RPC_URL","JITO_ENABLED","FLASHBOTS_PROTECT_ENABLED",
            "GRID_ENABLED","FUNDING_ARB_ENABLED","PENDLE_ENABLED","ROBINHOOD_ENABLED"]
    masked = {k: ("********" if os.environ.get(k) else "") for k in keys}
    has_value = {k: bool(os.environ.get(k)) for k in keys}
    return {"masked": masked, "set": has_value}

@app.post("/api/settings/basic")
async def save_settings_basic_stub(): return {"saved": {}, "demo": True}
@app.post("/api/settings/advanced")
async def save_settings_advanced_stub(): return {"saved": False, "demo": True}
@app.post("/api/bot/backtest/trigger")
async def backtest_trigger_stub():
    """Run a real backtest on the VPS."""
    import subprocess as _sp
    script = Path(__file__).parent.parent / "backtest.py"
    output = Path(__file__).parent.parent / "data" / "backtest_last.json"
    log = Path("/tmp/backtest_demo.log")
    venv_py = Path(__file__).parent.parent / "venv" / "bin" / "python3"
    # Write wrapper that validates JSON before overwriting results
    wrapper = Path("/tmp/backtest_demo_wrapper.sh")
    wrapper.write_text(
        f"#!/bin/bash\n"
        f"{venv_py} -u {script} --mode all --min-score 83 --days 7 --json > {log} 2>&1\n"
        f'LAST=$(tail -1 {log})\n'
        f'echo "$LAST" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null\n'
        f'if [ $? -eq 0 ]; then echo "$LAST" > {output}; fi\n'
    )
    wrapper.chmod(0o755)
    try:
        proc = _sp.Popen(["bash", str(wrapper)], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        return {"status": "launched", "pid": str(proc.pid), "mode": "all", "min_score": 83, "days": 7}
    except Exception as e:
        return {"status": "error", "message": str(e)}
@app.post("/api/bot/channels")
async def add_channel_stub(): return {"ok": False, "demo": True}
@app.post("/api/bot/grid/start")
async def grid_start_stub(): return {"ok": False, "demo": True}
@app.post("/api/bot/grid/stop")
async def grid_stop_stub(): return {"ok": False, "demo": True}
@app.post("/api/bot/positions/close")
async def close_position_stub(): return {"ok": False, "demo": True}
@app.post("/api/bot/rebalance/confirm")
async def rebalance_confirm_stub(): return {"ok": False, "demo": True}

@app.websocket("/api/ws/alerts")
async def ws_alerts_stub(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await asyncio.sleep(30)
            try: await websocket.send_json({"type": "ping"})
            except: break
    except: pass


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

_panel_dist = Path(__file__).parent / "panel_dist"
if _panel_dist.exists() and (_panel_dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_panel_dist / "assets")), name="panel_assets")


# ── Setup Wizard Endpoints ────────────────────────────────────────────────────

class SetupSaveBody(BaseModel):
    step: str = ""
    values: dict = {}

class SetupTestBody(BaseModel):
    token: str = ""
    chat_id: str = ""


@app.get("/api/setup/status")
def api_setup_status():
    """Returns whether the first-run setup wizard has been completed,
    and how far through setup the user got (for resuming mid-wizard)."""
    conn = get_db_rw()
    if not conn:
        return JSONResponse({"setup_complete": False, "current_step": 0,
                             "credentials_saved": False, "telegram_configured": False})
    try:
        def cfg(key):
            row = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
            return (row[0] or "").strip() if row else ""

        setup_complete      = cfg("setup_complete") == "1"
        telegram_configured = bool(cfg("telegram_bot_token") and cfg("telegram_chat_id"))
        credentials_saved   = telegram_configured and bool(cfg("coinbase_api_key"))

        # Determine which step the user last completed so wizard can resume
        step = 0
        if cfg("telegram_bot_token"): step = max(step, 2)
        if cfg("coinbase_api_key"):   step = max(step, 3)
        if cfg("kraken_api_key"):     step = max(step, 4)
        if cfg("base_private_key"):   step = max(step, 5)
        if cfg("portfolio_total_usd"):step = max(step, 6)
        if setup_complete:            step = 0   # wizard is done — don't resume it

        return JSONResponse({
            "setup_complete":      setup_complete,
            "current_step":        step,
            "credentials_saved":   credentials_saved,
            "telegram_configured": telegram_configured,
        })
    finally:
        conn.close()


@app.post("/api/setup/save")
def api_setup_save(body: SetupSaveBody):
    """Saves a wizard step's key/value pairs to the bot_config table.
    Called once per step as the user progresses through the wizard."""
    if not body.values:
        return JSONResponse({"ok": False, "error": "No values provided"})

    conn = get_db_rw()
    if not conn:
        return JSONResponse({"ok": False, "error": "Database unavailable"})
    try:
        for key, value in body.values.items():
            # Only allow safe key names — no SQL injection via key names
            if not all(c.isalnum() or c == "_" for c in key):
                continue
            conn.execute(
                "INSERT INTO bot_config (key, value, updated_at) "
                "VALUES (?,?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value))
            )
        conn.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.post("/api/setup/test-telegram")
async def api_setup_test_telegram(body: SetupTestBody):
    """Sends a test message via the Telegram Bot API to verify the token and chat ID.
    Uses httpx directly — no extra dependencies beyond what the dashboard already has."""
    token   = body.token.strip()
    chat_id = body.chat_id.strip()
    if not token or not chat_id:
        return JSONResponse({"ok": False, "error": "Token and chat ID are required"})
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id,
                      "text": "✅ Breadbot setup test — Telegram is connected!"}
            )
            data = r.json()
            if data.get("ok"):
                return JSONResponse({"ok": True})
            return JSONResponse({"ok": False, "error": data.get("description", "Unknown error")})
    except httpx.TimeoutException:
        return JSONResponse({"ok": False, "error": "Request timed out — check your token"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/terms.html")
def serve_terms():
    t = Path(__file__).parent / "panel_dist" / "terms.html"
    if t.exists(): return FileResponse(str(t))
    return JSONResponse({"error": "Not found"}, status_code=404)

@app.get("/")
@app.get("/{path:path}")
def serve_frontend(path: str = ""):
    panel_index = Path(__file__).parent / "panel_dist" / "index.html"
    if panel_index.exists():
        return FileResponse(str(panel_index),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})
    index = Path(__file__).parent / "static" / "index.html"
    if index.exists(): return FileResponse(str(index))
    return JSONResponse({"error": "Frontend not ready"}, status_code=503)


if __name__ == "__main__":
    print("\n  Breadbot Terminal")
    print("  -----------------")
    print(f"  Database : {DB_PATH}")
    print(f"  DB found : {DB_PATH.exists()}")
    print(f"  URL      : http://localhost:8000\n")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
