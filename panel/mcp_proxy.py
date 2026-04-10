"""
MCP Proxy
All bot communication flows through here. The panel never calls the bot directly.
Every action is a named MCP tool call — the bot exposes only what it explicitly allows.
"""

import os
import json
import hmac
import hashlib
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from auth import verify_session

router = APIRouter()


def _mcp_url() -> str:
    return os.environ.get("MCP_SERVER_URL", "http://localhost:8051")


def _mcp_secret() -> str:
    return os.environ.get("MCP_SECRET", "")


def _sign_request(payload: dict) -> str:
    """HMAC-SHA256 signature so the bot can verify the call came from the panel."""
    body = json.dumps(payload, sort_keys=True)
    sig = hmac.new(_mcp_secret().encode(), body.encode(), hashlib.sha256).hexdigest()
    return sig



def _unwrap_result(result: Any) -> Any:
    """Unwrap FastMCP content wrapper to plain dict/value."""
    import json as _json
    if isinstance(result, dict) and "structuredContent" in result and result["structuredContent"]:
        return result["structuredContent"]
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
        if content and isinstance(content, list) and isinstance(content[0], dict) and "text" in content[0]:
            try:
                return _json.loads(content[0]["text"])
            except (_json.JSONDecodeError, TypeError):
                return content[0]["text"]
    return result

async def call_tool(tool_name: str, params: dict = None) -> Any:
    """Core MCP tool call. Uses FastMCP streamable-HTTP with session initialization."""
    params = params or {}

    init_payload = {
        "jsonrpc": "2.0", "method": "initialize", "id": 0,
        "params": {"protocolVersion": "2024-11-05",
                   "capabilities": {},
                   "clientInfo": {"name": "breadbot-panel", "version": "1.0"}}
    }
    call_payload = {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": tool_name, "arguments": params}
    }
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Step 1: initialize to get session ID
            ir = await client.post(f"{_mcp_url()}/mcp", json=init_payload, headers=hdrs)
            session_id = ir.headers.get("mcp-session-id") or ir.headers.get("x-mcp-session-id")
            if session_id:
                hdrs["mcp-session-id"] = session_id
            # Step 2: call the tool
            resp = await client.post(f"{_mcp_url()}/mcp", json=call_payload, headers=hdrs)
        if resp.status_code == 401:
            raise HTTPException(status_code=502, detail="MCP authentication failed")
        if resp.status_code not in (200, 202):
            raise HTTPException(status_code=502, detail=f"Bot returned {resp.status_code}")

        # FastMCP may return SSE stream or plain JSON
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            # Parse SSE: find the data line with the JSON-RPC result
            result = None
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    import json as _json
                    try:
                        msg = _json.loads(line[6:])
                        if "result" in msg:
                            result = msg["result"]
                            break
                        if "error" in msg:
                            raise HTTPException(status_code=502, detail=msg["error"].get("message", "MCP error"))
                    except _json.JSONDecodeError:
                        continue
            return _unwrap_result(result) if result is not None else {}
        else:
            data = resp.json()
            if "error" in data:
                raise HTTPException(status_code=502, detail=data["error"].get("message", "MCP error"))
            result = data.get("result", {})
            return _unwrap_result(result)

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach bot: {str(e)}")


# ── read endpoints ────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(auth=Depends(verify_session)):
    data = await call_tool("get_status")
    # Normalise field names to match what the React dashboard expects
    if isinstance(data, dict):
        # trading_active is the inverse of trading_paused
        if "trading_paused" in data and "trading_active" not in data:
            data["trading_active"] = not data["trading_paused"]
        # Alias today realised PnL
        if "today_realized_pnl" in data and "total_pnl" not in data:
            data["total_pnl"] = data["today_realized_pnl"]
        # Inject config fields the dashboard expects if missing
        if "max_position_size_pct" not in data:
            try:
                import os as _os
                data["max_position_size_pct"] = float(_os.getenv("MAX_POSITION_SIZE_PCT", "0.02"))
            except Exception:
                data["max_position_size_pct"] = 0.02
        if "max_positions" not in data:
            try:
                import os as _os
                data["max_positions"] = int(_os.getenv("MAX_OPEN_POSITIONS", "5"))
            except Exception:
                data["max_positions"] = 5
    return data


@router.get("/positions")
async def get_positions(auth=Depends(verify_session)):
    data = await call_tool("get_positions")
    raw = data if isinstance(data, list) else data.get("positions", []) if isinstance(data, dict) else []
    return {"positions": raw}


@router.get("/yields")
async def get_yields(auth=Depends(verify_session)):
    data = await call_tool("get_yields")
    raw = data if isinstance(data, list) else data.get("yields", []) if isinstance(data, dict) else []
    platforms = []
    for d in raw:
        if isinstance(d, dict):
            platforms.append({
                "platform": d.get("platform", ""),
                "apy": d.get("apy", 0),
                "type": d.get("asset", "USDC"),
                "current": False,
            })
    return {"platforms": platforms, "rebalance_threshold": float(os.environ.get("REBALANCE_THRESHOLD_PCT", "1.5"))}


@router.get("/alerts/history")
async def get_alert_history(auth=Depends(verify_session)):
    data = await call_tool("get_alert_history")
    # MCP returns raw DB rows as a list — React expects {"alerts": [...]} with mapped fields
    raw = data if isinstance(data, list) else data.get("alerts", []) if isinstance(data, dict) else []
    import json as _jf, time as _t
    from datetime import datetime as _dt
    alerts = []
    for d in raw:
        ts = 0
        try:
            dt = _dt.fromisoformat(d.get("created_at", ""))
            ts = int(dt.timestamp())
        except Exception:
            ts = int(_t.time())
        flags = []
        rf = d.get("rug_flags", "") or ""
        if rf:
            try:
                fl = _jf.loads(rf) if rf.startswith("[") else [s.strip() for s in rf.split(",") if s.strip()]
            except Exception:
                fl = [s.strip() for s in rf.split(",") if s.strip()]
            for f in fl:
                lo = f.lower()
                ft = "risk" if any(w in lo for w in ["honeypot","blacklist","pause","proxy","high tax","pumped"]) else "warn" if any(w in lo for w in ["mint","owner","concentrated","not locked","unlocked"]) else "ok"
                flags.append({"label": f, "type": ft})
        decided = d.get("decision", "pending") not in ("pending", "")
        alerts.append({
            "id": d.get("id"), "chain": d.get("chain", ""), "token": d.get("token_name", d.get("symbol", "")),
            "symbol": d.get("symbol", ""), "contract": d.get("token_addr", ""),
            "security_score": d.get("rug_score", 0), "price": d.get("price_usd"),
            "liquidity_usd": d.get("liquidity"), "volume_24h": d.get("volume_24h"),
            "market_cap": d.get("mcap"), "age_hours": None, "position_size_usd": None,
            "source": "Scanner", "timestamp": ts, "expires_at": ts + 900,
            "flags": flags, "actioned": decided,
            "action": d.get("decision") if decided else None,
        })
    return {"alerts": alerts}


@router.get("/pnl")
async def get_pnl(auth=Depends(verify_session)):
    data = await call_tool("daily_pnl")
    # Alias realized_pnl_usd -> total_pnl for dashboard compatibility
    if isinstance(data, dict) and "realized_pnl_usd" in data and "total_pnl" not in data:
        data["total_pnl"] = data["realized_pnl_usd"]
    return data


@router.get("/risk")
async def get_risk(auth=Depends(verify_session)):
    return await call_tool("get_risk_status")


# ── write endpoints (all require confirmation in frontend) ────────────────────

@router.post("/pause")
async def pause_trading(auth=Depends(verify_session)):
    return await call_tool("pause_trading")


@router.post("/resume")
async def resume_trading(auth=Depends(verify_session)):
    return await call_tool("resume_trading")


class TradeDecisionPayload(BaseModel):
    alert_id: str
    action: str  # "buy" | "skip"


@router.post("/decision")
async def trade_decision(payload: TradeDecisionPayload, auth=Depends(verify_session)):
    if payload.action not in ("buy", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'buy' or 'skip'")
    return await call_tool("record_decision", {"alert_id": payload.alert_id, "action": payload.action})


class ClosePositionPayload(BaseModel):
    position_id: str


@router.post("/positions/close")
async def close_position(payload: ClosePositionPayload, auth=Depends(verify_session)):
    return await call_tool("close_position", {"position_id": payload.position_id})


class RugCheckPayload(BaseModel):
    address: str


@router.post("/rugcheck")
async def rug_check(payload: RugCheckPayload, auth=Depends(verify_session)):
    return await call_tool("run_rug_check", {"address": payload.address})


@router.post("/rebalance/confirm")
async def rebalance_confirm(auth=Depends(verify_session)):
    return await call_tool("confirm_rebalance")


# ── Signal Channels ───────────────────────────────────────────────────────────

class AddChannelPayload(BaseModel):
    channel_id: str
    label: str = ""


@router.get("/channels")
async def list_channels(auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {"secret": os.environ.get("MCP_SECRET", ""), "action": "list"})


@router.get("/channels/hits")
async def channel_hits(auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {"secret": os.environ.get("MCP_SECRET", ""), "action": "hits"})


@router.post("/channels")
async def add_channel(payload: AddChannelPayload, auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {
        "secret": os.environ.get("MCP_SECRET", ""),
        "action": "add",
        "channel_id": payload.channel_id,
        "label": payload.label,
    })



# ── Grid trading ──────────────────────────────────────────────────────────────

@router.get("/grid/status")
async def grid_status(auth=Depends(verify_session)):
    return await call_tool("get_grid_status")


@router.post("/grid/start")
async def grid_start(auth=Depends(verify_session)):
    return await call_tool("grid_command", {"subcommand": "start"})


@router.post("/grid/stop")
async def grid_stop(auth=Depends(verify_session)):
    return await call_tool("grid_command", {"subcommand": "stop"})


# ── Funding rate arb ──────────────────────────────────────────────────────────

@router.get("/funding/rates")
async def funding_rates(auth=Depends(verify_session)):
    data = await call_tool("get_funding_rates")
    arb_exchange = os.environ.get("FUNDING_ARB_EXCHANGE", "bybit")
    entry_t = float(os.environ.get("FUNDING_RATE_ENTRY_THRESHOLD", "0.01"))
    exit_t = float(os.environ.get("FUNDING_RATE_EXIT_THRESHOLD", "0.005"))
    arb_enabled = os.environ.get("FUNDING_ARB_ENABLED", "false").lower() in ("true", "1")
    venue_map = {"bybit": ("Bybit", "amber", None), "binance": ("Binance.US", "amber", True), "coinbase_cfm": ("Coinbase CFM", "green", True)}
    vl, vc, vlu = venue_map.get(arb_exchange, (arb_exchange, "amber", None))
    raw = data if isinstance(data, list) else data.get("rates", []) if isinstance(data, dict) else []
    rates = []
    for d in raw:
        if isinstance(d, dict):
            rate = d.get("rate_8h", d.get("rate", 0)) or 0
            rates.append({"pair": (d.get("pair","") or "").replace("USDT","").replace("/",""), "rate_8h_pct": rate, "annualized_pct": rate*3*365, "above_entry": abs(rate) >= entry_t})
    return {"arb_exchange": arb_exchange, "venue_label": vl, "venue_color": vc, "venue_legal_us": vlu, "arb_enabled": arb_enabled, "entry_threshold_pct": entry_t, "exit_threshold_pct": exit_t, "rates": rates}


@router.get("/funding/positions")
async def funding_positions(auth=Depends(verify_session)):
    data = await call_tool("get_funding_positions")
    raw = data if isinstance(data, list) else data.get("positions", []) if isinstance(data, dict) else []
    return {"positions": raw}


# ── Strategy performance ──────────────────────────────────────────────────────

@router.get("/strategy/performance")
async def strategy_performance(auth=Depends(verify_session)):
    return await call_tool("get_strategy_performance")


@router.get("/pnl/history")
async def pnl_history(days: int = 30, auth=Depends(verify_session)):
    return await call_tool("pnl_history", {"days": days})

@router.delete("/channels/{channel_id}")
async def remove_channel(channel_id: str, auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {
        "secret": os.environ.get("MCP_SECRET", ""),
        "action": "remove",
        "channel_id": channel_id,
    })


# ── Backtesting ───────────────────────────────────────────────────────────────

class TriggerBacktestPayload(BaseModel):
    mode:      str = "all"
    min_score: int = 75
    days:      int = 30


@router.get("/backtest/results")
async def backtest_results(auth=Depends(verify_session)):
    return await call_tool("get_backtest_results")


@router.post("/backtest/trigger")
async def backtest_trigger(payload: TriggerBacktestPayload, auth=Depends(verify_session)):
    return await call_tool("trigger_backtest", {
        "mode":      payload.mode,
        "min_score": payload.min_score,
        "days":      payload.days,
    })
