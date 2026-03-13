"""
mcp_server.py — Breadbot MCP Server (Phase 3)

Exposes bot data as MCP tools for Claude Desktop.
READ-ONLY: never writes to the database or executes trades.

Run: python mcp_server.py
Test: npx @modelcontextprotocol/inspector python mcp_server.py
"""

import sqlite3
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastmcp import FastMCP

# Import rug check scorer directly
from scanner.rugcheck import score_token

mcp = FastMCP("breadbot")

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query and return rows as dicts."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


# ── Tool 1: get_status ──────────────────────────────────────────────────────

@mcp.tool()
def get_status() -> dict:
    """Get current Breadbot status: trading state, today's P&L, open positions, daily loss limit usage, and last activity timestamp."""

    # Open positions count
    positions = _query("SELECT COUNT(*) as cnt FROM positions WHERE status='open'")
    open_count = positions[0]["cnt"] if positions and "cnt" in positions[0] else 0

    # Today's realized P&L from closed trades
    pnl_rows = _query(
        "SELECT COALESCE(SUM(pnl_usd), 0) as total_pnl FROM trades WHERE DATE(executed_at) = DATE('now')"
    )
    daily_pnl = pnl_rows[0]["total_pnl"] if pnl_rows and "total_pnl" in pnl_rows[0] else 0.0

    # Daily loss limit from config defaults
    portfolio_usd = 5000.0
    daily_loss_limit_pct = 0.05
    try:
        import config
        portfolio_usd = config.TOTAL_PORTFOLIO_USD
        daily_loss_limit_pct = config.DAILY_LOSS_LIMIT_PCT
    except Exception:
        pass

    daily_loss_limit_usd = portfolio_usd * daily_loss_limit_pct
    loss_pct_used = round(abs(daily_pnl) / daily_loss_limit_usd * 100, 1) if daily_pnl < 0 else 0.0

    # Determine if trading would be paused
    trading_active = True
    pause_reason = ""
    if daily_pnl <= -daily_loss_limit_usd:
        trading_active = False
        pause_reason = f"Daily loss limit hit: ${abs(daily_pnl):.2f} (limit: ${daily_loss_limit_usd:.2f})"

    # Last activity timestamp (most recent across key tables)
    last_activity = _query(
        """SELECT MAX(ts) as last_ts FROM (
            SELECT MAX(created_at) as ts FROM meme_alerts
            UNION ALL
            SELECT MAX(executed_at) as ts FROM trades
            UNION ALL
            SELECT MAX(recorded_at) as ts FROM yield_snapshots
        )"""
    )
    last_ts = last_activity[0]["last_ts"] if last_activity and last_activity[0].get("last_ts") else "no data"

    return {
        "trading_active": trading_active,
        "pause_reason": pause_reason,
        "daily_pnl_usd": round(daily_pnl, 2),
        "daily_loss_limit_usd": daily_loss_limit_usd,
        "daily_loss_pct_used": loss_pct_used,
        "open_positions": open_count,
        "portfolio_usd": portfolio_usd,
        "last_activity": last_ts,
    }


# ── Tool 2: get_positions ───────────────────────────────────────────────────

@mcp.tool()
def get_positions() -> dict:
    """Get all open meme coin positions with entry price, stop loss, profit targets, and time opened."""

    rows = _query(
        """SELECT symbol, token_name, chain, token_addr, entry_price, quantity,
                  cost_basis_usd, stop_loss_usd, take_profit_25, take_profit_50,
                  exchange, opened_at
           FROM positions WHERE status='open' ORDER BY opened_at DESC"""
    )

    if not rows or (len(rows) == 1 and "error" in rows[0]):
        return {"status": "no data", "positions": []}

    return {"count": len(rows), "positions": rows}


# ── Tool 3: get_yields ──────────────────────────────────────────────────────

@mcp.tool()
def get_yields() -> dict:
    """Get current stablecoin yield rates (APY) across all monitored DeFi platforms: Coinbase, Morpho, Aave V3, Compound, Kraken."""

    rows = _query(
        """SELECT platform, asset, apy, tvl_usd, notes, recorded_at
           FROM yield_snapshots
           WHERE id IN (
               SELECT MAX(id) FROM yield_snapshots GROUP BY platform, asset
           )
           ORDER BY apy DESC"""
    )

    if not rows or (len(rows) == 1 and "error" in rows[0]):
        return {"status": "no data", "yields": []}

    return {"count": len(rows), "yields": rows}


# ── Tool 4: run_rug_check ───────────────────────────────────────────────────

@mcp.tool()
async def run_rug_check(contract_address: str, chain: str) -> dict:
    """Run an on-demand rug pull security check on any token contract address.
    Calls GoPlus and RugCheck APIs to produce a security score (0-100) with detailed flags.

    Args:
        contract_address: The token contract address to check.
        chain: The blockchain - either "solana" or "base".
    """
    if chain not in ("solana", "base"):
        return {"error": f"Unsupported chain: {chain}. Use 'solana' or 'base'."}

    try:
        result = await score_token(contract_address, chain)
        # Simplify the raw data for readability
        output = {
            "contract": contract_address,
            "chain": chain,
            "security_score": result["score"],
            "flags": result["flags"],
            "flag_count": len(result["flags"]),
            "verdict": "PASS" if result["score"] >= 50 else "BLOCKED",
        }
        # Include GoPlus summary if available
        raw = result.get("raw", {})
        if "goplus" in raw and raw["goplus"]:
            gp = raw["goplus"]
            output["goplus_summary"] = {
                "is_honeypot": gp.get("is_honeypot", "unknown"),
                "buy_tax": gp.get("buy_tax", "unknown"),
                "sell_tax": gp.get("sell_tax", "unknown"),
                "holder_count": gp.get("holder_count", "unknown"),
            }
        if "rugcheck" in raw:
            output["rugcheck_passed"] = raw["rugcheck"]
        return output

    except Exception as e:
        return {"error": f"Rug check failed: {str(e)}"}


# ── Tool 5: get_alert_history ────────────────────────────────────────────────

@mcp.tool()
def get_alert_history(limit: int = 20) -> dict:
    """Get recent scanner alerts showing tokens discovered, their security scores, and buy/skip decisions.

    Args:
        limit: Number of alerts to return (default 20, max 100).
    """
    limit = min(max(1, limit), 100)

    rows = _query(
        """SELECT id, symbol, token_name, chain, token_addr, price_usd, liquidity,
                  volume_24h, mcap, rug_score, rug_flags, decision, created_at
           FROM meme_alerts ORDER BY created_at DESC LIMIT ?""",
        (limit,)
    )

    if not rows or (len(rows) == 1 and "error" in rows[0]):
        return {"status": "no data", "alerts": []}

    return {"count": len(rows), "alerts": rows}


# ── Tool 6: get_yield_history ────────────────────────────────────────────────

@mcp.tool()
def get_yield_history(platform: str, hours: int = 24) -> dict:
    """Get historical yield rate readings for a specific platform over time.

    Args:
        platform: Platform name (e.g. "Coinbase", "Aave V3", "Compound V3", "Kraken", "Coinbase Morpho").
        hours: Number of hours of history to return (default 24).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    rows = _query(
        """SELECT platform, asset, apy, tvl_usd, recorded_at
           FROM yield_snapshots
           WHERE platform = ? AND recorded_at >= ?
           ORDER BY recorded_at ASC""",
        (platform, cutoff)
    )

    if not rows or (len(rows) == 1 and "error" in rows[0]):
        return {"status": "no data", "platform": platform, "hours": hours, "readings": []}

    return {
        "platform": platform,
        "hours": hours,
        "reading_count": len(rows),
        "readings": rows,
    }


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
