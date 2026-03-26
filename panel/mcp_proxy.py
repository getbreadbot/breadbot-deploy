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


async def call_tool(tool_name: str, params: dict = None) -> Any:
    """Core MCP tool call. Raises HTTPException on failure."""
    params = params or {}
    payload = {"tool": tool_name, "params": params, "ts": int(time.time())}
    sig = _sign_request(payload)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_mcp_url()}/call",
                json=payload,
                headers={"X-Signature": sig, "Content-Type": "application/json"},
            )
        if resp.status_code == 401:
            raise HTTPException(status_code=502, detail="MCP authentication failed — check MCP_SECRET")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Bot returned {resp.status_code}")
        return resp.json()
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
