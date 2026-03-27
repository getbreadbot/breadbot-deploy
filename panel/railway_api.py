"""
Railway API
Read and write environment variables for the bot service via the Railway API.
The panel writes settings changes here so they persist across bot restarts.
"""

import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from auth import verify_session

router = APIRouter()

RAILWAY_API = "https://backboard.railway.app/graphql/v2"

# These are the only env vars the panel is allowed to write.
# This is an explicit allowlist — the panel cannot overwrite arbitrary vars.
BASIC_VARS = {
    "MAX_POSITION_SIZE_PCT",
    "DAILY_LOSS_LIMIT_PCT",
    "MIN_LIQUIDITY_USD",
    "MIN_VOLUME_24H_USD",
    "AUTO_EXECUTE_MIN_SCORE",
    "ALERT_CHANNEL",
    "AUTO_EXECUTE",
}

ADVANCED_VARS = {
    "COINBASE_API_KEY", "COINBASE_SECRET_KEY",
    "KRAKEN_API_KEY", "KRAKEN_SECRET_KEY",
    "BYBIT_API_KEY", "BYBIT_SECRET_KEY",
    "BINANCE_API_KEY", "BINANCE_SECRET_KEY",
    "GEMINI_API_KEY", "GEMINI_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "SOLANA_RPC_URL", "EVM_BASE_RPC_URL",
    "JITO_ENABLED", "FLASHBOTS_PROTECT_ENABLED",
    "JITO_TIP_LAMPORTS",
    "YIELD_REBALANCE_ENABLED", "YIELD_REBALANCE_MODE",
    "REBALANCE_THRESHOLD_PCT", "REBALANCE_MIN_AMOUNT_USD",
    "REBALANCE_MAX_GAS_USD",
    # Grid trading
    "GRID_ENABLED", "GRID_PAIR", "GRID_UPPER_PCT", "GRID_LOWER_PCT",
    "GRID_NUM_LEVELS", "GRID_ALLOCATION_USD", "GRID_EXCHANGE", "GRID_RSI_GUARD",
    # Funding rate arb
    "FUNDING_ARB_ENABLED", "FUNDING_ARB_PAIRS", "FUNDING_ARB_ALLOCATION_PCT",
    "FUNDING_ARB_EXCHANGE", "FUNDING_RATE_ENTRY_THRESHOLD", "FUNDING_RATE_EXIT_THRESHOLD",
    # Pendle
    "PENDLE_ENABLED", "PENDLE_CHAIN", "PENDLE_MIN_RATE", "PENDLE_MAX_TERM_DAYS",
    # Robinhood
    "ROBINHOOD_ENABLED",
    # Coinbase CFM perpetuals
    "COINBASE_PERP_ENABLED",
    # Drift Protocol
    "DRIFT_ENABLED", "DRIFT_MARKET_PAIRS",
}

ALL_WRITABLE = BASIC_VARS | ADVANCED_VARS


def _headers() -> dict:
    token = os.environ.get("RAILWAY_API_TOKEN", "")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _service_id() -> str:
    return os.environ.get("RAILWAY_SERVICE_ID", "")


def _project_id() -> str:
    return os.environ.get("RAILWAY_PROJECT_ID", "")


async def write_env_var(key: str, value: str) -> bool:
    """Write a single env var to the bot's Railway service. Returns True on success."""
    token = os.environ.get("RAILWAY_API_TOKEN")
    if not token:
        return False

    mutation = """
    mutation UpsertEnvVar($input: VariableUpsertInput!) {
        variableUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "projectId": _project_id(),
            "serviceId": _service_id(),
            "name": key,
            "value": value,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                RAILWAY_API,
                json={"query": mutation, "variables": variables},
                headers=_headers(),
            )
        return resp.status_code == 200
    except httpx.RequestError:
        return False


async def read_env_vars(keys: list[str]) -> dict:
    """Read current values of specific env vars from the Railway service."""
    token = os.environ.get("RAILWAY_API_TOKEN")
    if not token:
        # Fall back to reading from local environment (works for bot service)
        return {k: os.environ.get(k, "") for k in keys}

    query = """
    query GetVariables($projectId: String!, $serviceId: String!) {
        variables(projectId: $projectId, serviceId: $serviceId) {
            edges { node { name value } }
        }
    }
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                RAILWAY_API,
                json={"query": query, "variables": {"projectId": _project_id(), "serviceId": _service_id()}},
                headers=_headers(),
            )
        if resp.status_code != 200:
            return {k: os.environ.get(k, "") for k in keys}

        data = resp.json()
        edges = data.get("data", {}).get("variables", {}).get("edges", [])
        all_vars = {e["node"]["name"]: e["node"]["value"] for e in edges}
        return {k: all_vars.get(k, os.environ.get(k, "")) for k in keys}
    except Exception:
        return {k: os.environ.get(k, "") for k in keys}


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("/basic")
async def get_basic_settings(auth=Depends(verify_session)):
    values = await read_env_vars(list(BASIC_VARS))
    return values


@router.get("/advanced")
async def get_advanced_settings(auth=Depends(verify_session)):
    values = await read_env_vars(list(ADVANCED_VARS))
    # Mask all values — frontend reveals on click
    masked = {k: ("••••••••" if v else "") for k, v in values.items()}
    # Also send whether each key has a value set (for status indicators)
    has_value = {k: bool(v) for k, v in values.items()}
    return {"masked": masked, "set": has_value}


@router.get("/advanced/reveal/{key}")
async def reveal_setting(key: str, auth=Depends(verify_session)):
    if key not in ADVANCED_VARS:
        raise HTTPException(status_code=400, detail="Key not in advanced settings")
    values = await read_env_vars([key])
    return {"key": key, "value": values.get(key, "")}


class SaveBasicPayload(BaseModel):
    settings: dict[str, str]


@router.post("/basic")
async def save_basic_settings(payload: SaveBasicPayload, auth=Depends(verify_session)):
    disallowed = set(payload.settings.keys()) - BASIC_VARS
    if disallowed:
        raise HTTPException(status_code=400, detail=f"Not writable via basic settings: {disallowed}")

    results = {}
    for key, value in payload.settings.items():
        ok = await write_env_var(key, value)
        os.environ[key] = value  # Update local environment immediately
        results[key] = ok

    return {"saved": results}


class SaveAdvancedPayload(BaseModel):
    key: str
    value: str


@router.post("/advanced")
async def save_advanced_setting(payload: SaveAdvancedPayload, auth=Depends(verify_session)):
    if payload.key not in ADVANCED_VARS:
        raise HTTPException(status_code=400, detail="Key not in advanced settings allowlist")

    ok = await write_env_var(payload.key, payload.value)
    if ok:
        os.environ[payload.key] = payload.value

    return {"saved": ok, "key": payload.key}
