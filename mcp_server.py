#!/usr/bin/env python3
"""
mcp_server.py -- Phase 5
FastMCP server that exposes Breadbot data as MCP tools.
Claude connects to this server and gains read access to live bot state:
positions, yield rates, scanner alerts, risk status, and daily P&L.

Two write tools (pause_trading, resume_trading) require the MCP_SECRET.

Auth: shared secret in .env (MCP_SECRET). Server listens on localhost only.
Run:  python3 mcp_server.py

New .env vars:
  MCP_PORT   -- port to listen on (default 8051)
  MCP_SECRET -- shared secret for write ops (store in Vaultwarden -> Breadbot)
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH    = Path(__file__).parent / "data" / "cryptobot.db"
MCP_PORT   = int(os.getenv("MCP_PORT",   "8051"))
MCP_SECRET = os.getenv("MCP_SECRET",     "").strip()
PORTFOLIO  = float(os.getenv("TOTAL_PORTFOLIO_USD", "5000"))

mcp = FastMCP(
    name="Breadbot",
    instructions=(
        "Live data from the Breadbot crypto trading bot. "
        "Use get_status for a quick overview. "
        "Write operations (pause_trading, resume_trading) require MCP_SECRET."
    ),
)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise RuntimeError(f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def _db_write() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def _rows(query: str, params: tuple = ()) -> list[dict]:
    conn = _db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_status() -> dict[str, Any]:
    "Snapshot of current bot state: paused flag, open positions, today P&L, loss limit consumed."
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _db()
    paused_row = conn.execute(
        "SELECT value FROM bot_config WHERE key='trading_paused'"
    ).fetchone()
    paused = (paused_row["value"].lower() in ("1", "true", "yes")) if paused_row else False
    open_count = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='open'"
    ).fetchone()[0]
    summary = conn.execute(
        "SELECT realized_pnl, fees_paid, trades_count FROM daily_summary WHERE date=?",
        (today,),
    ).fetchone()
    conn.close()
    realized_pnl = dict(summary)["realized_pnl"] if summary else 0.0
    fees_paid    = dict(summary)["fees_paid"]    if summary else 0.0
    trades_count = dict(summary)["trades_count"] if summary else 0
    daily_loss_limit = PORTFOLIO * float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
    loss_consumed_pct = round(abs(min(realized_pnl, 0)) / daily_loss_limit * 100, 1) if daily_loss_limit else 0
    return {
        "trading_paused":          paused,
        "open_positions":          open_count,
        "today_realized_pnl":      round(realized_pnl, 2),
        "today_fees_paid":         round(fees_paid, 2),
        "today_trades":            trades_count,
        "daily_loss_limit_usd":    round(daily_loss_limit, 2),
        "loss_limit_consumed_pct": loss_consumed_pct,
        "portfolio_usd":           PORTFOLIO,
        "timestamp_utc":           datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
def get_positions(status: str = "open") -> list[dict]:
    "Return positions. status: open (default), closed, or all."
    if status == "all":
        where, params = "", ()
    else:
        where, params = "WHERE status=?", (status,)
    return _rows(
        f"SELECT * FROM positions {where} ORDER BY opened_at DESC LIMIT 50",
        params,
    )


@mcp.tool()
def get_yields(limit: int = 20) -> list[dict]:
    "Most recent yield snapshot per platform/asset. Shows current APY across all monitored platforms."
    return _rows(
        """
        SELECT platform, asset, apy, tvl_usd, notes, recorded_at
        FROM yield_snapshots
        WHERE (platform, asset, recorded_at) IN (
            SELECT platform, asset, MAX(recorded_at)
            FROM yield_snapshots
            GROUP BY platform, asset
        )
        ORDER BY apy DESC
        LIMIT ?
        """,
        (limit,),
    )


@mcp.tool()
def get_yield_history(platform: str, asset: str = "USDC", limit: int = 48) -> list[dict]:
    "Yield rate readings for a specific platform over time. limit=48 gives ~2 days of hourly data."
    return _rows(
        "SELECT platform, asset, apy, tvl_usd, notes, recorded_at "
        "FROM yield_snapshots WHERE platform=? AND asset=? "
        "ORDER BY recorded_at DESC LIMIT ?",
        (platform, asset, limit),
    )


@mcp.tool()
def get_alert_history(limit: int = 20, decision: str = "") -> list[dict]:
    "Recent scanner alerts. decision: buy, skip, pending, or empty for all."
    if decision:
        return _rows(
            "SELECT * FROM meme_alerts WHERE decision=? ORDER BY created_at DESC LIMIT ?",
            (decision, limit),
        )
    return _rows(
        "SELECT * FROM meme_alerts ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )


@mcp.tool()
def run_rug_check(token_address: str, chain: str = "solana") -> dict[str, Any]:
    "On-demand rug pull check via GoPlus. chain: solana or base."
    goplus_key  = os.getenv("GOPLUS_API_KEY", "")
    chain_id_map = {"base": "8453", "ethereum": "1"}
    if chain.lower() == "solana":
        url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={token_address}"
    else:
        cid = chain_id_map.get(chain.lower(), "8453")
        url = f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={token_address}"
    headers = {"Authorization": goplus_key} if goplus_key else {}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        result_data = (data.get("result") or {}).get(token_address.lower()) or \
                      (data.get("result") or {}).get(token_address, {})
        return {"token_address": token_address, "chain": chain, "raw": result_data,
                "checked_at": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"error": str(e), "token_address": token_address, "chain": chain}


# ---------------------------------------------------------------------------
# Write tools (require MCP_SECRET)
# ---------------------------------------------------------------------------
def _auth(secret: str) -> bool:
    if not MCP_SECRET:
        return False
    return secret == MCP_SECRET


@mcp.tool()
def pause_trading(secret: str, reason: str = "") -> dict[str, str]:
    "Pause all new trade alerts. Requires MCP_SECRET. reason is optional."
    if not _auth(secret):
        return {"status": "error", "message": "Invalid secret. Trading not paused."}
    conn = _db_write()
    now  = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES ('trading_paused', '1', ?)",
        (now,),
    )
    if reason:
        conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES ('pause_reason', ?, ?)",
            (reason, now),
        )
    conn.commit()
    conn.close()
    return {"status": "paused", "reason": reason, "timestamp_utc": now}


@mcp.tool()
def resume_trading(secret: str) -> dict[str, str]:
    "Resume trading after a pause. Requires MCP_SECRET."
    if not _auth(secret):
        return {"status": "error", "message": "Invalid secret. Trading not resumed."}
    conn = _db_write()
    now  = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES ('trading_paused', '0', ?)",
        (now,),
    )
    conn.commit()
    conn.close()
    return {"status": "resumed", "timestamp_utc": now}



# ---------------------------------------------------------------------------
# Panel tools — added 2026-03-26
# ---------------------------------------------------------------------------

@mcp.tool()
def daily_pnl() -> dict[str, Any]:
    "Today's realized P&L and trade count."
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = _rows(
        "SELECT realized_pnl, unrealized_pnl, yield_earned, fees_paid, trades_count "
        "FROM daily_summary WHERE date=?",
        (today,),
    )
    if rows:
        r = rows[0]
        return {
            "date": today,
            "realized_pnl_usd":   round(r["realized_pnl"],   2),
            "unrealized_pnl_usd": round(r["unrealized_pnl"], 2),
            "yield_earned_usd":   round(r["yield_earned"],    2),
            "fees_paid_usd":      round(r["fees_paid"],       2),
            "trades_count":       r["trades_count"],
        }
    return {
        "date": today,
        "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0,
        "yield_earned_usd": 0.0, "fees_paid_usd": 0.0, "trades_count": 0,
    }


@mcp.tool()
def get_risk_status() -> dict[str, Any]:
    "Risk manager state: loss limit, position limits, pause state, daily loss consumed."
    daily_loss_limit_pct  = float(os.getenv("DAILY_LOSS_LIMIT_PCT",   "0.05"))
    max_position_pct      = float(os.getenv("MAX_POSITION_SIZE_PCT",  "0.02"))
    max_open_positions    = int(os.getenv("MAX_OPEN_POSITIONS",       "5"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = _rows(
        "SELECT realized_pnl FROM daily_summary WHERE date=?", (today,)
    )
    realized_pnl       = summary[0]["realized_pnl"] if summary else 0.0
    daily_loss_limit_usd = PORTFOLIO * daily_loss_limit_pct
    loss_consumed_pct    = (
        round(abs(min(realized_pnl, 0)) / daily_loss_limit_usd * 100, 1)
        if daily_loss_limit_usd else 0.0
    )
    open_count = _rows("SELECT COUNT(*) as cnt FROM positions WHERE status='open'")
    open_positions = open_count[0]["cnt"] if open_count else 0
    paused_row     = _rows("SELECT value FROM bot_config WHERE key='trading_paused'")
    paused         = bool(paused_row and paused_row[0]["value"].lower() in ("1", "true", "yes"))
    reason_row     = _rows("SELECT value FROM bot_config WHERE key='pause_reason'")
    pause_reason   = reason_row[0]["value"] if reason_row else ""
    return {
        "trading_paused":           paused,
        "pause_reason":             pause_reason,
        "daily_loss_limit_pct":     daily_loss_limit_pct,
        "daily_loss_limit_usd":     round(daily_loss_limit_usd, 2),
        "daily_loss_consumed_pct":  loss_consumed_pct,
        "max_position_size_pct":    max_position_pct,
        "max_open_positions":       max_open_positions,
        "current_open_positions":   open_positions,
        "portfolio_usd":            PORTFOLIO,
    }


@mcp.tool()
def record_decision(secret: str, alert_id: int, action: str) -> dict[str, str]:
    "Log buy or skip decision for a scanner alert. action must be 'buy' or 'skip'. Requires MCP_SECRET."
    if not _auth(secret):
        return {"status": "error", "message": "Invalid secret."}
    if action not in ("buy", "skip"):
        return {"status": "error", "message": "action must be 'buy' or 'skip'."}
    conn = _db_write()
    conn.execute(
        "UPDATE meme_alerts SET decision=? WHERE id=?",
        (action, alert_id),
    )
    conn.commit()
    affected = conn.total_changes
    conn.close()
    if affected == 0:
        return {"status": "error", "message": f"No alert found with id={alert_id}."}
    now = datetime.now(timezone.utc).isoformat()
    return {"status": "ok", "alert_id": alert_id, "decision": action, "timestamp_utc": now}


@mcp.tool()
def close_position(secret: str, position_id: int) -> dict[str, str]:
    """Request a market close for an open position.
    Writes a flag to bot_config; the bot executes the market sell on its next cycle.
    Requires MCP_SECRET."""
    if not _auth(secret):
        return {"status": "error", "message": "Invalid secret."}
    rows = _rows(
        "SELECT id, symbol, chain FROM positions WHERE id=? AND status='open'",
        (position_id,),
    )
    if not rows:
        return {"status": "error", "message": f"No open position found with id={position_id}."}
    conn = _db_write()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, '1', ?)",
        (f"close_requested_{position_id}", now),
    )
    conn.commit()
    conn.close()
    pos = rows[0]
    return {
        "status":       "close_requested",
        "position_id":  position_id,
        "symbol":       pos["symbol"],
        "chain":        pos["chain"],
        "timestamp_utc": now,
        "note":         "Bot will execute market sell on next cycle.",
    }


@mcp.tool()
def confirm_rebalance(
    secret: str, from_platform: str, to_platform: str, amount_usd: float
) -> dict[str, str]:
    """Request the yield rebalancer to move stablecoin funds between platforms.
    Writes a flag to bot_config; the rebalancer executes on its next cycle.
    Requires MCP_SECRET."""
    if not _auth(secret):
        return {"status": "error", "message": "Invalid secret."}
    if amount_usd <= 0:
        return {"status": "error", "message": "amount_usd must be positive."}
    import json as _json
    now     = datetime.now(timezone.utc).isoformat()
    payload = _json.dumps({
        "from_platform": from_platform,
        "to_platform":   to_platform,
        "amount_usd":    amount_usd,
        "requested_at":  now,
    })
    conn = _db_write()
    conn.execute(
        "INSERT OR REPLACE INTO bot_config (key, value, updated_at) "
        "VALUES ('rebalance_requested', ?, ?)",
        (payload, now),
    )
    conn.commit()
    conn.close()
    return {
        "status":        "rebalance_requested",
        "from_platform": from_platform,
        "to_platform":   to_platform,
        "amount_usd":    amount_usd,
        "timestamp_utc": now,
        "note":          "Yield rebalancer will execute on next cycle.",
    }

# ---------------------------------------------------------------------------
# Alpha Channel management
# ---------------------------------------------------------------------------

def _ensure_alpha_channels_table() -> None:
    conn = _db_write()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alpha_channels (
            channel_id  TEXT PRIMARY KEY,
            label       TEXT NOT NULL DEFAULT '',
            added_at    TEXT NOT NULL DEFAULT (datetime('now')),
            active      INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


@mcp.tool()
def manage_alpha_channels(
    secret: str,
    action: str,
    channel_id: str = "",
    label: str = "",
) -> dict:
    """Manage Telegram alpha channels monitored by the social signals layer.

    Actions:
      list   -- return all channels (active and inactive)
      add    -- add a new channel (channel_id required; label optional)
      remove -- deactivate a channel (channel_id required; sets active=0)
      hits   -- return last 20 alpha hits from alt_data_signals
    """
    if not _auth(secret):
        return {"status": "error", "message": "Invalid secret."}

    _ensure_alpha_channels_table()

    if action == "list":
        conn = _db()
        rows = conn.execute(
            "SELECT channel_id, label, added_at, active FROM alpha_channels ORDER BY added_at DESC"
        ).fetchall()
        conn.close()
        return {"status": "ok", "channels": [dict(r) for r in rows]}

    elif action == "add":
        if not channel_id:
            return {"status": "error", "message": "channel_id is required for add."}
        now = datetime.now(timezone.utc).isoformat()
        conn = _db_write()
        existing = conn.execute(
            "SELECT channel_id, label, active FROM alpha_channels WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE alpha_channels SET active=1, label=?, added_at=? WHERE channel_id=?",
                (label or existing["label"], now, channel_id),
            )
            conn.commit()
            conn.close()
            return {"status": "reactivated", "channel_id": channel_id}
        conn.execute(
            "INSERT INTO alpha_channels (channel_id, label, added_at, active) VALUES (?, ?, ?, 1)",
            (channel_id, label or f"Channel {channel_id}", now),
        )
        conn.commit()
        conn.close()
        return {"status": "added", "channel_id": channel_id, "label": label or f"Channel {channel_id}"}

    elif action == "remove":
        if not channel_id:
            return {"status": "error", "message": "channel_id is required for remove."}
        conn = _db_write()
        conn.execute("UPDATE alpha_channels SET active=0 WHERE channel_id=?", (channel_id,))
        conn.commit()
        conn.close()
        return {"status": "deactivated", "channel_id": channel_id}

    elif action == "hits":
        conn = _db()
        rows = conn.execute(
            """SELECT timestamp, market_id, description, value, scanner_triggered
               FROM alt_data_signals
               WHERE source = 'alpha_channel'
               ORDER BY timestamp DESC LIMIT 20"""
        ).fetchall()
        conn.close()
        return {"status": "ok", "hits": [dict(r) for r in rows]}

    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use list, add, remove, or hits."}



# ---------------------------------------------------------------------------
# Grid trading tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_grid_status() -> dict:
    """Return current grid engine state, RSI, price vs range, cycles, profit."""
    try:
        from grid_engine import GridEngine, trend_guard, GRID_ENABLED, GRID_PAIR
        import os
        conn = _db()
        session = conn.execute(
            "SELECT * FROM grid_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        fills = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(net_profit_usd) as profit FROM grid_fills WHERE session_id = ?",
            (session["id"] if session else 0,)
        ).fetchone() if session else None
        conn.close()

        blocked, rsi = trend_guard(GRID_PAIR)
        return {
            "enabled": GRID_ENABLED,
            "pair": GRID_PAIR,
            "rsi": round(rsi, 2),
            "trend_guard_blocked": blocked,
            "state": session["state"] if session else "STANDBY",
            "entry_price": session["entry_price"] if session else None,
            "upper_bound": session["upper_bound"] if session else None,
            "lower_bound": session["lower_bound"] if session else None,
            "num_levels": session["num_levels"] if session else int(os.getenv("GRID_NUM_LEVELS", 20)),
            "allocation_usd": float(os.getenv("GRID_ALLOCATION_USD", 500)),
            "cycles_completed": fills["cnt"] if fills else 0,
            "total_profit_usd": round(fills["profit"] or 0, 4) if fills else 0,
        }
    except Exception as e:
        return {"error": str(e), "enabled": False, "state": "STANDBY"}


@mcp.tool()
def grid_command(subcommand: str) -> dict:
    """Execute a grid command: start or stop."""
    from grid_engine import GridEngine
    # Grid engine is a singleton managed by main.py — proxy via scanner module
    try:
        from scanner import _grid_engine
        if subcommand == "start":
            result = _grid_engine.start()
        elif subcommand == "stop":
            result = _grid_engine.stop()
        else:
            return {"error": f"Unknown subcommand: {subcommand}"}
        return {"status": "ok", "message": result}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Funding rate arb tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_funding_rates() -> dict:
    """Return current funding rates for all monitored pairs."""
    try:
        from funding_arb_engine import get_funding_rates as _get_rates, ARB_PAIRS, ENTRY_THRESHOLD, EXIT_THRESHOLD, ARB_ENABLED
        rates = _get_rates(ARB_PAIRS)
        result = []
        for pair, rate in rates.items():
            ann = rate * 3 * 365 * 100
            result.append({
                "pair": pair,
                "rate_8h_pct": round(rate * 100, 5),
                "annualized_pct": round(ann, 2),
                "above_entry": rate * 100 >= ENTRY_THRESHOLD,
            })
        return {
            "rates": result,
            "entry_threshold_pct": ENTRY_THRESHOLD,
            "exit_threshold_pct": EXIT_THRESHOLD,
            "arb_enabled": ARB_ENABLED,
        }
    except Exception as e:
        return {"error": str(e), "rates": []}


@mcp.tool()
def get_funding_positions() -> dict:
    """Return open funding arb positions with cumulative income."""
    try:
        from funding_arb_engine import db_get_open_positions
        rows = db_get_open_positions()
        total = sum(r["funding_collected"] for r in rows)
        return {"positions": rows, "total_funding_collected_usd": round(total, 4), "count": len(rows)}
    except Exception as e:
        return {"error": str(e), "positions": []}


# ---------------------------------------------------------------------------
# Strategy performance summary
# ---------------------------------------------------------------------------

@mcp.tool()
def get_strategy_performance() -> dict:
    """Return 30-day P&L summary for all three strategy engines."""
    from datetime import datetime, timezone, timedelta
    conn = _db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    # Grid performance
    grid = conn.execute(
        """SELECT COUNT(*) as cycles, SUM(net_profit_usd) as profit,
           SUM(buy_price * quantity) as volume
           FROM grid_fills WHERE completed_at > ?""",
        (cutoff,)
    ).fetchone()

    # Funding arb performance
    funding_open = conn.execute(
        """SELECT COUNT(*) as cnt, SUM(funding_collected) as collected
           FROM funding_positions WHERE opened_at > ? AND closed_at IS NULL""",
        (cutoff,)
    ).fetchone()
    funding_closed = conn.execute(
        """SELECT COUNT(*) as cnt, SUM(realized_pnl) as pnl, SUM(funding_collected) as collected
           FROM funding_positions WHERE opened_at > ? AND closed_at IS NOT NULL""",
        (cutoff,)
    ).fetchone()

    # Yield rebalancer
    rebalance = conn.execute(
        """SELECT COUNT(*) as cnt, SUM(net_gain_usd) as gain
           FROM rebalance_events WHERE timestamp > ?""",
        (cutoff,)
    ).fetchone()

    conn.close()
    return {
        "period_days": 30,
        "grid": {
            "cycles": grid["cycles"] or 0,
            "profit_usd": round(grid["profit"] or 0, 4),
            "volume_usd": round(grid["volume"] or 0, 2),
        },
        "funding_arb": {
            "open_positions": funding_open["cnt"] or 0,
            "funding_collected_usd": round((funding_open["collected"] or 0) + (funding_closed["collected"] or 0), 4),
            "closed_pnl_usd": round(funding_closed["pnl"] or 0, 4),
        },
        "yield_rebalancer": {
            "rebalances": rebalance["cnt"] or 0,
            "yield_gained_usd": round(rebalance["gain"] or 0, 4),
        },
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Breadbot MCP server | port={MCP_PORT} | db={DB_PATH}")
    print(f"Secret configured: {'yes' if MCP_SECRET else 'NO -- set MCP_SECRET in .env'}")
    mcp.run(transport="streamable-http", host="127.0.0.1", port=MCP_PORT)
