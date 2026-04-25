"""
Research / Watchlist endpoints (S70 P2).

Mounted at /api/research on the panel and reused (read-only) on the
demo dashboard.

Exposes:
  GET    /api/research/{addr}      — run on-demand security check
  GET    /api/research/watchlist   — list watched coins
  POST   /api/research/watchlist   — add coin to watchlist
  DELETE /api/research/watchlist/{id}
                                   — remove coin from watchlist
  PATCH  /api/research/watchlist/{id}
                                   — update alert thresholds
  POST   /api/research/buy         — open a position for the given address

The /research/{addr} handler proxies the original logic from
dashboard/server.py — GoPlus + RugCheck + DEXScreener — without taking a
dependency on the dashboard service. Both surfaces use the same logic so
demo and panel return identical scores.

Auth: all endpoints require a valid panel session. The buy endpoint is
extra-restricted: it goes through AutoExecutor.evaluate() so it cannot
trade while the bot is paused or the daily loss limit is hit, and then
calls execute_trade(force=True) so the user-initiated buy works
regardless of execution_mode.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import verify_session

log = logging.getLogger(__name__)

router = APIRouter()

# ── DB resolution ────────────────────────────────────────────────────────────
DB_PATH = (
    Path(os.environ.get("BREADBOT_DB_PATH"))
    if os.environ.get("BREADBOT_DB_PATH")
    else Path(__file__).parent.parent / "data" / "cryptobot.db"
)

# Allow imports from project root for AutoExecutor + execute_trade
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="bot database not found")
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _detect_chain(addr: str) -> tuple[str, str]:
    """Return (chain, chain_id) tuple. EVM addresses start with 0x."""
    if addr.startswith("0x"):
        return "base", "8453"
    return "solana", "solana"


# ── Research (proxy of dashboard/server.py:api_research) ─────────────────────

async def _run_research(token_addr: str) -> dict:
    """Run the same security checks as the legacy dashboard endpoint.

    Reuses the same scoring logic (matching dashboard/server.py) so the
    demo and the panel return identical scores. Any future score-rule
    changes should be made in BOTH places — there's no shared module
    today because the dashboard runs in a different service.
    """
    chain, chain_id = _detect_chain(token_addr)
    result = {
        "token_addr":  token_addr,
        "chain":       chain,
        "rug_score":   100,
        "flags":       [],
        "goplus":      {},
        "rugcheck":    {},
        "dexscreener": {},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        # ── GoPlus ───────────────────────────────────────────────────────
        try:
            gp_resp = await client.get(
                f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}",
                params={"contract_addresses": token_addr},
            )
            gp_data = gp_resp.json()
            gp_root = gp_data.get("result", {}) or {}
            gp = gp_root.get(token_addr.lower()) or gp_root.get(token_addr) or {}

            result["goplus"] = {
                "is_honeypot":  str(gp.get("is_honeypot", "0")) == "1",
                "sell_tax":     float(gp.get("sell_tax", 0) or 0),
                "buy_tax":      float(gp.get("buy_tax", 0) or 0),
                "owner_address": gp.get("owner_address", ""),
            }
            flags: list[str] = []
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
        except Exception as exc:
            log.warning("research: GoPlus fetch failed for %s: %s", token_addr, exc)

        # ── RugCheck ─────────────────────────────────────────────────────
        try:
            rc_resp = await client.get(
                f"https://api.rugcheck.xyz/v1/tokens/{token_addr}/report"
            )
            if rc_resp.status_code == 200:
                rc_data = rc_resp.json()
                risks = rc_data.get("risks", []) or []
                result["rugcheck"] = {
                    "score":  rc_data.get("score", 0),
                    "risks":  [r.get("name", "") for r in risks if r.get("name")],
                }
                if any(r.get("level") == "critical" for r in risks):
                    result["flags"].append("RugCheck critical risk")
                    result["rug_score"] = max(0, result["rug_score"] - 15)
        except Exception as exc:
            log.warning("research: RugCheck fetch failed for %s: %s", token_addr, exc)

        # ── DEXScreener ──────────────────────────────────────────────────
        try:
            dx_resp = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
            )
            dx_data = dx_resp.json()
            pairs = dx_data.get("pairs") or []
            if pairs:
                p = pairs[0]
                result["dexscreener"] = {
                    "name":       p.get("baseToken", {}).get("name", ""),
                    "symbol":     p.get("baseToken", {}).get("symbol", ""),
                    "price_usd":  float(p.get("priceUsd") or 0),
                    "liquidity":  float((p.get("liquidity") or {}).get("usd") or 0),
                    "volume_24h": float((p.get("volume") or {}).get("h24") or 0),
                    "market_cap": float(p.get("marketCap") or p.get("fdv") or 0),
                }
        except Exception as exc:
            log.warning("research: DEXScreener fetch failed for %s: %s", token_addr, exc)

    return result


@router.get("/{token_addr}")
async def research(token_addr: str, _: bool = Depends(verify_session)):
    return await _run_research(token_addr)


# ── Watchlist CRUD ───────────────────────────────────────────────────────────

class WatchlistAdd(BaseModel):
    address:           str  = Field(..., min_length=8, max_length=80)
    chain:             Optional[str] = None        # auto-detected if not given
    symbol:            Optional[str] = None
    name:              Optional[str] = None
    alert_score_drop:  Optional[int] = Field(None, ge=1, le=100)
    alert_price_pct:   Optional[float] = Field(None, ge=0.01, le=10.0)


class WatchlistUpdate(BaseModel):
    alert_score_drop:  Optional[int]   = Field(None, ge=1, le=100)
    alert_price_pct:   Optional[float] = Field(None, ge=0.01, le=10.0)


@router.get("/watchlist/list")
def watchlist_list(_: bool = Depends(verify_session)):
    """List all watched coins with their last-seen score and price."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, address, chain, symbol, name, added_at,
                   last_score, last_price, last_checked_at,
                   alert_score_drop, alert_price_pct, last_alert_at
            FROM watchlist
            ORDER BY added_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@router.post("/watchlist/add")
def watchlist_add(payload: WatchlistAdd, _: bool = Depends(verify_session)):
    """Add a coin to the watchlist. Idempotent on (address, chain)."""
    chain = payload.chain or _detect_chain(payload.address)[0]
    conn = _connect()
    try:
        try:
            cur = conn.execute(
                """
                INSERT INTO watchlist
                  (address, chain, symbol, name,
                   alert_score_drop, alert_price_pct)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.address, chain, payload.symbol, payload.name,
                    payload.alert_score_drop or 15,
                    payload.alert_price_pct  or 0.20,
                ),
            )
            new_id = cur.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            # Already in the watchlist — return the existing row id
            row = conn.execute(
                "SELECT id FROM watchlist WHERE address=? AND chain=?",
                (payload.address, chain),
            ).fetchone()
            new_id = row["id"] if row else None
        return {"id": new_id, "ok": True}
    finally:
        conn.close()


@router.delete("/watchlist/{wl_id}")
def watchlist_delete(wl_id: int, _: bool = Depends(verify_session)):
    conn = _connect()
    try:
        conn.execute("DELETE FROM watchlist WHERE id=?", (wl_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.patch("/watchlist/{wl_id}")
def watchlist_patch(wl_id: int, payload: WatchlistUpdate,
                    _: bool = Depends(verify_session)):
    fields, params = [], []
    if payload.alert_score_drop is not None:
        fields.append("alert_score_drop=?"); params.append(payload.alert_score_drop)
    if payload.alert_price_pct is not None:
        fields.append("alert_price_pct=?");  params.append(payload.alert_price_pct)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    params.append(wl_id)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE watchlist SET {', '.join(fields)} WHERE id=?",
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── Manual buy ───────────────────────────────────────────────────────────────

class BuyRequest(BaseModel):
    address:    str   = Field(..., min_length=8, max_length=80)
    chain:      Optional[str] = None
    symbol:     Optional[str] = None
    score:      int   = Field(..., ge=0, le=100)
    market_cap: float = Field(0.0, ge=0)
    price_usd:  float = Field(..., gt=0)


@router.post("/buy")
def research_buy(payload: BuyRequest, _: bool = Depends(verify_session)):
    """
    Open a bot-managed position for a user-supplied address.

    Goes through AutoExecutor.evaluate() so the standard safety gates apply
    (paused flag, daily loss limit, daily trade cap, alt-data composite),
    then calls execute_trade(force=True) so the trade fires regardless of
    the global execution_mode setting (this is a manual user action).
    """
    chain = payload.chain or _detect_chain(payload.address)[0]

    # Lazy-import to avoid loading the entire bot stack at panel startup
    try:
        from auto_executor import AutoExecutor
        from exchange_executor import execute_trade
    except ImportError as exc:
        log.error("research_buy: failed to import bot modules: %s", exc)
        raise HTTPException(status_code=503, detail="bot modules unavailable")

    ae = AutoExecutor()
    decision = ae.evaluate({
        "score":      payload.score,
        "market_cap": payload.market_cap,
        "token":      payload.symbol or payload.address[:8],
        "chain":      chain,
        "price":      payload.price_usd,
    })

    # Hard blocks (paused, daily loss, daily cap, composite signal) → 409
    if decision.blocked:
        raise HTTPException(status_code=409, detail=decision.reason)

    # Soft blocks (score below threshold, market cap above limit) — for
    # manual buys we still respect them so the user knows. Returning 422
    # signals the UI to prompt: "Override?" — for now, hold the line and
    # surface the reason. P3 enhancement: an `override=True` flag.
    if not decision.executed:
        raise HTTPException(status_code=422, detail=decision.reason)

    ok = execute_trade(
        chain=chain,
        token_addr=payload.address,
        symbol=payload.symbol or payload.address[:8],
        position_usd=decision.position_usd,
        price_usd=payload.price_usd,
        force=True,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="trade execution failed")
    return {
        "ok": True,
        "position_usd": decision.position_usd,
        "strategy":     decision.strategy,
        "reason":       decision.reason,
    }
