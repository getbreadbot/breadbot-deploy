"""
bot_config_api.py — S81 P3
Canonical Settings endpoint backed by SQLite bot_config table.

Replaces the silently-broken /api/settings/basic endpoint that wrote to Railway env
(no-op on this VPS deployment) and panel-process os.environ (never seen by the
scanner process). Every key in BASIC_FIELDS reads/writes bot_config directly,
which is what the scanner actually reads via config._float_config / _bool_config /
_str_config.

Field schema is the source of truth — frontend renders types from here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import verify_session

router = APIRouter()

DB_PATH = Path(__file__).parent.parent / "data" / "cryptobot.db"


# Field schema — single source of truth for type, default, validation, UI group.
# `key` is the bot_config key (lowercase canonical form). `type` controls UI
# rendering and value coercion. `group` controls section ordering.
BASIC_FIELDS: list[dict[str, Any]] = [
    # ── Risk & sizing ────────────────────────────────────────────
    {
        "key": "max_position_size_pct",
        "label": "Max position size",
        "type": "float",
        "default": 0.02,
        "group": "Risk and sizing",
        "desc": "Maximum percent of portfolio placed on any single trade. 0.02 means 2 percent.",
        "suffix": "(0.02 = 2%)",
    },
    {
        "key": "daily_loss_limit_pct",
        "label": "Daily loss limit",
        "type": "float",
        "default": 0.05,
        "group": "Risk and sizing",
        "desc": "Bot pauses trading if losses hit this percent of portfolio in one day.",
        "suffix": "(0.02 = 2%)",
    },
    {
        "key": "portfolio_total_usd",
        "label": "Portfolio total",
        "type": "float",
        "default": 5000.0,
        "group": "Risk and sizing",
        "desc": "Total portfolio value in USD. Used to compute position size and daily loss cap.",
        "suffix": "USD",
    },
    {
        "key": "min_liquidity_usd",
        "label": "Minimum liquidity",
        "type": "float",
        "default": 22000.0,
        "group": "Risk and sizing",
        "desc": "Tokens with pool liquidity below this are filtered out.",
        "suffix": "USD",
    },
    {
        "key": "min_volume_24h_usd",
        "label": "Minimum 24h volume",
        "type": "float",
        "default": 40000.0,
        "group": "Risk and sizing",
        "desc": "Tokens with 24-hour trading volume below this are filtered out.",
        "suffix": "USD",
    },
    {
        "key": "min_rug_score",
        "label": "Minimum security score",
        "type": "float",
        "default": 46.0,
        "group": "Risk and sizing",
        "desc": "Tokens scoring below this on security checks never reach Telegram.",
        "suffix": "out of 100",
    },

    # ── Execution ────────────────────────────────────────────────
    {
        "key": "execution_mode",
        "label": "Execution mode",
        "type": "enum",
        "options": ["manual", "auto"],
        "default": "manual",
        "group": "Execution",
        "desc": "Manual: every alert needs your tap to fire. Auto: high-score alerts execute without confirmation.",
    },
    {
        "key": "auto_strategy",
        "label": "Auto strategy",
        "type": "enum",
        "options": ["conservative", "balanced", "aggressive"],
        "default": "conservative",
        "group": "Execution",
        "desc": "Conservative requires score 96+. Balanced requires 85+. Aggressive requires 75+.",
    },
    {
        "key": "auto_max_trades_day",
        "label": "Max auto-trades per day",
        "type": "int",
        "default": 8,
        "group": "Execution",
        "desc": "Hard ceiling on trades the auto-executor can fire in one UTC day.",
    },

    # ── Safety ───────────────────────────────────────────────────
    {
        "key": "trading_paused",
        "label": "Trading paused",
        "type": "bool",
        "default": False,
        "group": "Safety",
        "desc": "When on, scanner runs but no alerts trigger trades. Same as /pause in Telegram.",
    },
    {
        "key": "TIME_STOP_ENABLED",
        "label": "Time-stop on losers",
        "type": "bool",
        "default": False,
        "group": "Safety",
        "desc": "Closes positions automatically after a configured time if still underwater.",
    },
    {
        "key": "pullback_enabled",
        "label": "Pullback wait",
        "type": "bool",
        "default": True,
        "group": "Safety",
        "desc": "On auto-fire, wait briefly for a price pullback before placing the order.",
    },

    # ── Notifications ────────────────────────────────────────────
    {
        "key": "notify_yield_changes",
        "label": "Yield change alerts",
        "type": "bool",
        "default": False,
        "group": "Notifications",
        "desc": "Telegram alert when stablecoin or LST yields move more than 0.5 percent.",
    },
    {
        "key": "alert_channel",
        "label": "Alert delivery channel",
        "type": "enum",
        "options": ["both", "telegram", "panel"],
        "default": "both",
        "group": "Notifications",
        "desc": "Where trade alerts are delivered: Telegram only, panel only, or both.",
    },
    {
        "key": "trailing_stop_enabled",
        "label": "Trailing stop",
        "type": "bool",
        "default": True,
        "group": "Safety",
        "desc": "Once a position gains past the activation threshold, the stop-loss follows price upward to lock in profit.",
    },
    {
        "key": "trailing_stop_activation_pct",
        "label": "Trail activation (%)",
        "type": "float",
        "default": 10.0,
        "group": "Safety",
        "desc": "Position must gain this percentage before the trailing stop engages.",
    },
    {
        "key": "trailing_stop_distance_pct",
        "label": "Trail distance (%)",
        "type": "float",
        "default": 8.0,
        "group": "Safety",
        "desc": "How far below the running high the trailing stop sits.",
    },
]


# Allowlist of writable keys — anything not in here will 400 on POST.
WRITABLE_KEYS = {f["key"] for f in BASIC_FIELDS}


def _get_db_value(key: str) -> str | None:
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT value FROM bot_config WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _set_db_value(key: str, value: str) -> bool:
    if not DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO bot_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now')",
            (key, value),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error:
        return False


def _coerce_for_storage(field: dict, raw: Any) -> str:
    """Convert incoming JSON value to the canonical string form stored in bot_config.

    Booleans always store as "true"/"false" lowercase to match _bool_config _TRUE_VALUES.
    Floats and ints store as str(value). Enums store the raw string after lowercase normalization.
    """
    t = field["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        s = str(raw).strip().lower()
        if s in {"true", "1", "yes", "on"}:
            return "true"
        if s in {"false", "0", "no", "off"}:
            return "false"
        raise ValueError(f"invalid bool: {raw!r}")
    if t == "float":
        return str(float(raw))
    if t == "int":
        return str(int(raw))
    if t == "enum":
        s = str(raw).strip().lower()
        if s not in {opt.lower() for opt in field["options"]}:
            raise ValueError(
                f"invalid option: {raw!r} not in {field['options']}"
            )
        return s
    return str(raw).strip()


def _decode_for_response(field: dict, raw: str | None) -> Any:
    """Convert stored string back to native type for the JSON response."""
    if raw is None or raw == "":
        return field["default"]
    t = field["type"]
    try:
        if t == "bool":
            return raw.strip().lower() in {"true", "1", "yes", "on"}
        if t == "float":
            return float(raw)
        if t == "int":
            return int(float(raw))  # tolerate "8.0"
        if t == "enum":
            return raw.strip().lower()
        return raw
    except (TypeError, ValueError):
        return field["default"]


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def get_config(auth=Depends(verify_session)):
    """Return all basic field schemas + current values, grouped by section."""
    values: dict[str, Any] = {}
    for f in BASIC_FIELDS:
        raw = _get_db_value(f["key"])
        values[f["key"]] = _decode_for_response(f, raw)
    return {
        "fields": BASIC_FIELDS,
        "values": values,
    }


class SaveConfigPayload(BaseModel):
    settings: dict[str, Any]


@router.post("")
async def save_config(
    payload: SaveConfigPayload,
    auth=Depends(verify_session),
):
    """Write a batch of bot_config keys. Rejects unknown keys outright."""
    disallowed = set(payload.settings.keys()) - WRITABLE_KEYS
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=f"Not writable via bot config endpoint: {sorted(disallowed)}",
        )

    field_by_key = {f["key"]: f for f in BASIC_FIELDS}
    results: dict[str, bool] = {}
    errors: dict[str, str] = {}

    for key, raw_value in payload.settings.items():
        field = field_by_key[key]
        try:
            stored = _coerce_for_storage(field, raw_value)
        except (ValueError, TypeError) as exc:
            results[key] = False
            errors[key] = str(exc)
            continue
        ok = _set_db_value(key, stored)
        results[key] = ok
        if not ok:
            errors[key] = "DB write failed"

    return {"saved": results, "errors": errors}
