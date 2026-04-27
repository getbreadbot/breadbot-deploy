"""
Strategy index endpoints (S73 P1).

Mounted at /api/bot on the panel. Provides a single round-trip GET that
returns enabled state + headline metric for all four strategy engines,
and a write-only POST to flip individual strategy enable flags in
bot_config.

Exposes:
  GET   /api/bot/strategy/all              — combined state for index page
  POST  /api/bot/strategy/{name}/enable    — flip {name}_enabled flag
                                             body: {"enabled": bool}

Strategy names are validated against a fixed allowlist:
  grid, funding_arb, yield_rebalance, pendle

The GET enriches the existing get_strategy_performance MCP tool output
with the current enable flag for each engine. The POST writes to
bot_config and returns the new value plus a short message — the engine
loops detect the transition within ~60s and start/stop in-process,
exactly the same pattern grid_enabled has used since S68 P3.

The grid endpoints in mcp_proxy.py (/api/bot/grid/start, /grid/stop) are
left in place — the per-strategy detail pages still use them. This file
adds the index-level endpoints without touching the existing routes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import verify_session
from mcp_proxy import call_tool

log = logging.getLogger(__name__)
router = APIRouter()

# Allowlist of strategies the index page controls. Names map directly to
# bot_config row keys: e.g. "grid" -> "grid_enabled". Keep this list in
# sync with the engines that poll bot_config (grid_engine.py,
# funding_arb_engine.py, yield_rebalancer.py, pendle_connector.py).
STRATEGIES = ("grid", "funding_arb", "yield_rebalance", "pendle")

# Display metadata used by the panel index. Kept here on the backend so
# any future label/description tweak ships in the JSON without a
# frontend rebuild.
STRATEGY_META: dict[str, dict[str, Any]] = {
    "grid": {
        "label": "Grid Trading",
        "description": "Buy/sell ladder within a price range. Profits from oscillation.",
        "configure_path": "/grid",
        "venue": "Binance.US",
    },
    "funding_arb": {
        "label": "Funding Arbitrage",
        "description": "Long spot + short perp to harvest funding payments.",
        "configure_path": "/funding",
        "venue": "Bybit / Coinbase CFM",
    },
    "yield_rebalance": {
        "label": "Yield Rebalancer",
        "description": "Auto-moves stablecoins to the highest-paying platform.",
        "configure_path": "/yields",
        "venue": "Aave / Compound / Spark / Kamino",
    },
    "pendle": {
        "label": "Pendle Fixed Yield",
        "description": "Locks in a fixed APY for a defined term via Pendle PT.",
        "configure_path": "/yields",
        "venue": "Pendle (Base / Arbitrum)",
    },
}


def _db_path() -> str:
    """Path to the main cryptobot.db. Same walk pattern mcp_proxy uses."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "data", "cryptobot.db"))


def _read_enabled_flags() -> dict[str, bool]:
    """Read all four {name}_enabled flags from bot_config in one query.

    Missing rows default to False — matches the engine-side defaults in
    grid_engine.is_enabled() etc.
    """
    keys = tuple(f"{n}_enabled" for n in STRATEGIES)
    placeholders = ",".join("?" * len(keys))
    out = {n: False for n in STRATEGIES}
    try:
        conn = sqlite3.connect(_db_path(), timeout=10)
        try:
            rows = conn.execute(
                f"SELECT key, value FROM bot_config WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("strategy_proxy: read bot_config failed: %s", exc)
        return out

    for key, value in rows:
        # Map "grid_enabled" -> "grid" etc.
        name = key[: -len("_enabled")] if key.endswith("_enabled") else key
        if name in out:
            out[name] = str(value).strip().lower() in ("true", "1", "yes", "on")
    return out


def _write_enabled_flag(name: str, value: bool) -> bool:
    """Write {name}_enabled to bot_config. Returns True on success."""
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy: {name}")
    key = f"{name}_enabled"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(_db_path(), timeout=10)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (key, "true" if value else "false", now),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        log.warning("strategy_proxy: write %s=%s failed: %s", key, value, exc)
        return False


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/strategy/all")
async def strategy_all(auth=Depends(verify_session)) -> dict[str, Any]:
    """Single round-trip for the panel index page.

    Returns:
      {
        "strategies": {
          "grid": {
            "enabled": bool, "label": str, "description": str,
            "configure_path": str, "venue": str, "metrics": {...}
          },
          ...
        }
      }

    metrics is the per-engine summary from get_strategy_performance,
    pass-through with safe defaults so the frontend can render before
    the MCP tool catches up.
    """
    flags = _read_enabled_flags()

    # Reuse the existing performance aggregator. It already normalises
    # the {grid, funding_arb, yield_rebalancer, pendle} shape, so we
    # don't duplicate that work here.
    try:
        perf = await call_tool("get_strategy_performance")
        if not isinstance(perf, dict):
            perf = {}
    except Exception as exc:
        log.warning("strategy_proxy: get_strategy_performance failed: %s", exc)
        perf = {}

    # The performance tool returns "yield_rebalancer" while our flag
    # name is "yield_rebalance". Normalise so the frontend sees one
    # naming scheme.
    perf_keymap = {
        "grid": "grid",
        "funding_arb": "funding_arb",
        "yield_rebalance": "yield_rebalancer",
        "pendle": "pendle",
    }

    out: dict[str, Any] = {}
    for name in STRATEGIES:
        meta = STRATEGY_META[name]
        metrics = perf.get(perf_keymap[name]) or {}
        if not isinstance(metrics, dict):
            metrics = {}
        out[name] = {
            "enabled": flags[name],
            "label": meta["label"],
            "description": meta["description"],
            "configure_path": meta["configure_path"],
            "venue": meta["venue"],
            "metrics": metrics,
        }
    return {"strategies": out}


class EnablePayload(BaseModel):
    enabled: bool


@router.post("/strategy/{name}/enable")
async def strategy_enable(
    name: str,
    payload: EnablePayload,
    auth=Depends(verify_session),
) -> dict[str, Any]:
    """Flip {name}_enabled in bot_config.

    The matching engine loop (grid_engine, funding_arb_engine, etc)
    polls bot_config every ~60s and starts/stops itself on transition.
    The response message reflects that — the UI should poll
    /strategy/all afterwards to confirm the engine actually started.
    """
    if name not in STRATEGIES:
        raise HTTPException(status_code=404, detail=f"unknown strategy: {name}")

    ok = _write_enabled_flag(name, payload.enabled)
    if not ok:
        raise HTTPException(status_code=500, detail="failed to write bot_config")

    verb = "enabled" if payload.enabled else "disabled"
    return {
        "status": "ok",
        "name": name,
        "enabled": payload.enabled,
        "message": (
            f"{STRATEGY_META[name]['label']} {verb}. "
            f"Engine will pick up the change within ~60s."
        ),
    }
