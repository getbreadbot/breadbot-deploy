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
    return await call_tool("get_status")


@router.get("/positions")
async def get_positions(auth=Depends(verify_session)):
    return await call_tool("get_positions")


@router.get("/yields")
async def get_yields(auth=Depends(verify_session)):
    return await call_tool("get_yields")


@router.get("/alerts/history")
async def get_alert_history(auth=Depends(verify_session)):
    return await call_tool("get_alert_history")


@router.get("/pnl")
async def get_pnl(auth=Depends(verify_session)):
    return await call_tool("daily_pnl")


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


@router.delete("/channels/{channel_id}")
async def remove_channel(channel_id: str, auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {
        "secret": os.environ.get("MCP_SECRET", ""),
        "action": "remove",
        "channel_id": channel_id,
    })
