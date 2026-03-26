"""
Push Notifications
Web Push API for delivering trade alerts when the panel tab is not open.
Uses VAPID keys — no third-party service required.
"""

import os
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

router = APIRouter()

# In-memory subscription store. On Railway, this resets on redeploy.
# For persistence, write subscriptions to the bot's SQLite via MCP.
_subscriptions: list[dict] = []


def _pywebpush_available() -> bool:
    try:
        import pywebpush
        return True
    except ImportError:
        return False


class PushSubscription(BaseModel):
    endpoint: str
    keys: dict  # {p256dh: str, auth: str}


@router.post("/subscribe")
async def subscribe(sub: PushSubscription, auth=Depends(__import__('auth').verify_session)):
    """Register a browser push subscription."""
    # Deduplicate by endpoint
    existing = [s for s in _subscriptions if s["endpoint"] == sub.endpoint]
    if not existing:
        _subscriptions.append(sub.dict())
    return {"subscribed": True, "total": len(_subscriptions)}


@router.delete("/subscribe")
async def unsubscribe(sub: PushSubscription, auth=Depends(__import__('auth').verify_session)):
    global _subscriptions
    _subscriptions = [s for s in _subscriptions if s["endpoint"] != sub.endpoint]
    return {"unsubscribed": True}


@router.get("/vapid-public-key")
async def get_vapid_key():
    key = os.environ.get("VAPID_PUBLIC_KEY", "")
    if not key:
        return {"key": None, "push_enabled": False}
    return {"key": key, "push_enabled": True}


async def push_alert_to_browsers(alert: dict):
    """
    Called by the WebSocket manager when a new alert arrives.
    Pushes to all registered browser subscriptions.
    """
    if not _subscriptions or not _pywebpush_available():
        return

    from pywebpush import webpush, WebPushException

    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    vapid_claims = {"sub": f"mailto:{os.environ.get('VAPID_EMAIL', 'admin@breadbot.app')}"}

    if not vapid_private:
        return

    payload = json.dumps({
        "title": f"Breadbot Alert — {alert.get('token', 'New Token')}",
        "body": f"Score {alert.get('security_score', '?')}/100 — {alert.get('chain', '')}",
        "alert_id": alert.get("id"),
    })

    dead = []
    for sub in _subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims=vapid_claims,
            )
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                dead.append(sub["endpoint"])
        except Exception:
            pass

    global _subscriptions
    _subscriptions = [s for s in _subscriptions if s["endpoint"] not in dead]
