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


# ── Research (uses shared research_logic.score_token, S72 P1) ────────────────

async def _run_research(token_addr: str) -> dict:
    """Run the full ~22-rule scoring rubric for a token address.

    S72 P1: this delegates to research_logic.score_token so the Research
    page sees the same score the scanner would compute. Before S72 this
    function ran a lighter 7-rule mechanical-safety rubric that started at
    100 and only deducted on hard rug-pull signals — the result was that
    nearly every coin scored 100 and the page was useless for real triage.

    The shared module surfaces GoPlus + RugCheck + DEXScreener data, plus
    the same momentum / liquidity / vol-liq / age / holder / social /
    Axiom / time-of-day adjustments scanner.process_pair applies. The
    `should_drop` flag is ignored here — the scanner uses it to skip an
    alert, the Research page wants the score regardless.
    """
    chain, _chain_id = _detect_chain(token_addr)
    result = {
        "token_addr":  token_addr,
        "chain":       chain,
        "rug_score":   100,
        "flags":       [],
        "goplus":      {},
        "rugcheck":    {},
        "dexscreener": {},
    }

    try:
        from research_logic import score_token
    except ImportError as exc:
        log.error("research_proxy: research_logic import failed: %s", exc)
        result["flags"] = ["Research scoring unavailable — internal error"]
        result["rug_score"] = 50
        return result

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            scored = await score_token(client, chain, token_addr)
    except Exception as exc:
        log.warning("research: score_token failed for %s: %s", token_addr, exc)
        result["flags"] = [f"Scoring failed: {exc}"]
        result["rug_score"] = 50
        return result

    result["rug_score"]   = scored.get("score", 50)
    result["flags"]       = scored.get("flags", [])
    result["goplus"]      = scored.get("goplus", {})
    result["rugcheck"]    = scored.get("rugcheck", {})
    result["dexscreener"] = scored.get("dexscreener", {})

    # ── Scanner cache lookup (S71 P2 — kept as historical context) ───────
    # If the scanner has already alerted on this address, surface what the
    # bot saw at that moment (alerted_at + flags). Now that the live score
    # above uses the same rubric, this block is "what we thought when we
    # alerted" rather than "the only place momentum signals show up".
    try:
        import json as _json
        with sqlite3.connect(str(DB_PATH), timeout=5) as conn:
            row = conn.execute(
                "SELECT rug_score, rug_flags, datetime(created_at) AS at "
                "FROM meme_alerts WHERE token_addr = ? "
                "ORDER BY id DESC LIMIT 1",
                (token_addr,),
            ).fetchone()
        if row is not None:
            scanner_score, scanner_flags_json, scanner_at = row
            try:
                scanner_flags = _json.loads(scanner_flags_json) if scanner_flags_json else []
            except (ValueError, TypeError):
                scanner_flags = []
            result["scanner_alert"] = {
                "score": scanner_score,
                "flags": scanner_flags,
                "alerted_at": scanner_at,
            }
    except Exception as exc:
        log.warning("research: scanner cache lookup failed for %s: %s", token_addr, exc)

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

    # S80 P6: record the position so SL/TP/time-stop/fast-poll/quote-sanity-guard
    # all activate. Prior to this, /buy executed the trade but never wrote a
    # row to positions, leaving the trade fully unmanaged.
    try:
        from scanner import record_position
        class _Result:
            def __init__(self, position_usd: float):
                self.position_usd = position_usd
        pair = {
            "chain":      chain,
            "token_addr": payload.address,
            "token_name": payload.symbol or payload.address[:8],
            "symbol":     payload.symbol or payload.address[:8],
            "price_usd":  payload.price_usd,
        }
        # Research-page buys have no source alert_id. Pass 0 — record_position
        # accepts it and does not enforce a foreign-key relationship.
        pos_id = record_position(pair, _Result(decision.position_usd), 0)
    except Exception as exc:
        log.error("research_buy: record_position failed (trade DID fire): %s", exc, exc_info=True)
        return {
            "ok":           True,
            "position_usd": decision.position_usd,
            "strategy":     decision.strategy,
            "reason":       decision.reason,
            "warning":      f"Trade fired but record_position failed: {exc}. "
                            f"Position is unmanaged — manual cleanup required.",
        }

    if pos_id is None:
        log.error("research_buy: record_position returned None (trade DID fire)")
        return {
            "ok":           True,
            "position_usd": decision.position_usd,
            "strategy":     decision.strategy,
            "reason":       decision.reason,
            "warning":      "Trade fired but position record returned None. "
                            "Position is unmanaged — manual cleanup required.",
        }

    return {
        "ok":           True,
        "position_id":  pos_id,
        "position_usd": decision.position_usd,
        "strategy":     decision.strategy,
        "reason":       decision.reason,
    }
