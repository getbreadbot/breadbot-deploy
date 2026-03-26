"""
Auth module
- First login: validates Whop license key, then sets a panel password
- Subsequent logins: password only, 7-day session cookie
- No Whop dependency after first login
"""

import os
import hashlib
import hmac
import secrets
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter()

SESSION_TTL = 60 * 60 * 24 * 7  # 7 days
SESSIONS: dict[str, float] = {}  # token → expiry timestamp


# ── helpers ──────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = os.environ.get("PANEL_SALT", "breadbot-default-salt")
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def _issue_token() -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + SESSION_TTL
    return token


def _purge_expired():
    now = time.time()
    expired = [t for t, exp in SESSIONS.items() if exp < now]
    for t in expired:
        del SESSIONS[t]


def verify_session(request: Request) -> bool:
    token = request.cookies.get("bb_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _purge_expired()
    if token not in SESSIONS or SESSIONS[token] < time.time():
        raise HTTPException(status_code=401, detail="Session expired")
    return True


# ── routes ───────────────────────────────────────────────────────────────────

class SetupPayload(BaseModel):
    license_key: str
    password: str


class LoginPayload(BaseModel):
    password: str


@router.get("/status")
async def auth_status():
    """
    Returns whether the panel has been set up yet.
    Frontend uses this to decide whether to show setup flow or login form.
    """
    configured = bool(os.environ.get("PANEL_PASSWORD_HASH"))
    return {"configured": configured}


@router.post("/setup")
async def setup(payload: SetupPayload, response: Response):
    """
    First-login flow. Validates the Whop license key, then stores the
    hashed panel password as PANEL_PASSWORD_HASH in the environment.
    Only works if PANEL_PASSWORD_HASH is not already set.
    """
    if os.environ.get("PANEL_PASSWORD_HASH"):
        raise HTTPException(status_code=400, detail="Panel is already configured")

    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Validate Whop license key (skip in operator mode)
    operator_mode = os.environ.get("OPERATOR_MODE", "").lower() in ("1", "true")
    whop_api_key = os.environ.get("WHOP_API_KEY")
    if whop_api_key and not operator_mode:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.whop.com/api/v2/me",
                    headers={"Authorization": f"Bearer {payload.license_key}"},
                )
            if resp.status_code != 200:
                raise HTTPException(status_code=403, detail="Invalid or expired license key")
        except httpx.RequestError:
            pass

    # Store hash in environment (persists for this process; Railway restart reads from vars)
    hashed = _hash_password(payload.password)
    os.environ["PANEL_PASSWORD_HASH"] = hashed

    # Write to Railway if token is available
    try:
        from railway_api import write_env_var
        await write_env_var("PANEL_PASSWORD_HASH", hashed)
    except Exception:
        pass  # Non-fatal — hash is in memory for this session

    token = _issue_token()
    response.set_cookie("bb_session", token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return {"ok": True}


@router.post("/login")
async def login(payload: LoginPayload, response: Response):
    stored_hash = os.environ.get("PANEL_PASSWORD_HASH")
    if not stored_hash:
        raise HTTPException(status_code=400, detail="Panel not configured — complete setup first")

    if _hash_password(payload.password) != stored_hash:
        raise HTTPException(status_code=401, detail="Incorrect password")

    token = _issue_token()
    response.set_cookie("bb_session", token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("bb_session")
    if token and token in SESSIONS:
        del SESSIONS[token]
    response.delete_cookie("bb_session")
    return {"ok": True}


@router.get("/me")
async def me(authenticated: bool = Depends(verify_session)):
    return {"authenticated": True}
