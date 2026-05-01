"""
WebSocket Manager
- Maintains active browser connections
- Polls the bot via MCP every 15 seconds for new alerts
- Fans out alerts to all connected browsers
- Holds missed alerts in memory for 15 minutes (for when panel tab was closed)
"""

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from auth import verify_session

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        # Pending alerts: list of {alert, timestamp, expired_at}
        self.pending: list[dict] = []
        self.ALERT_TTL = 60 * 15  # 15 minutes

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        # Send any non-expired pending alerts immediately on connect
        self._purge_expired()
        for item in self.pending:
            try:
                await ws.send_json({"type": "alert", "data": item["alert"], "queued": True})
            except Exception:
                pass

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def _purge_expired(self):
        now = time.time()
        self.pending = [p for p in self.pending if p["expires_at"] > now]

    async def queue_alert(self, alert: dict):
        """Store alert for delivery to browsers that connect later."""
        self._purge_expired()
        self.pending.append({
            "alert": alert,
            "queued_at": time.time(),
            "expires_at": time.time() + self.ALERT_TTL,
        })
        await self.broadcast({"type": "alert", "data": alert, "queued": False})

    def mark_actioned(self, alert_id: str):
        """Remove alert from pending once Buy/Skip has been pressed."""
        self.pending = [p for p in self.pending if p["alert"].get("id") != alert_id]

    async def poll_bot_alerts(self):
        """
        Background task. Polls MCP every 15 seconds for new alerts.
        The bot maintains a small queue of unactioned alerts — we diff against
        what we have already broadcast to avoid duplicates.
        """
        seen_ids: set[str] = set()

        while True:
            try:
                from mcp_proxy import call_tool
                result = await call_tool("get_alert_history")
                alerts = result.get("alerts", []) if isinstance(result, dict) else []

                for alert in alerts:
                    alert_id = alert.get("id")
                    if alert_id and alert_id not in seen_ids:
                        if not alert.get("actioned"):
                            seen_ids.add(alert_id)
                            await self.queue_alert(alert)

                # Broadcast a heartbeat so the frontend knows the connection is alive
                await self.broadcast({"type": "heartbeat", "ts": int(time.time())})

            except Exception:
                # Bot unreachable — broadcast a status update
                await self.broadcast({"type": "bot_offline"})

            await asyncio.sleep(15)


manager = ConnectionManager()


@router.websocket("/alerts")
async def ws_alerts(websocket: WebSocket):
    # Session cookie auth for WebSocket
    token = websocket.cookies.get("bb_session")
    if not token:
        await websocket.close(code=4001)
        return

    from auth import SESSIONS
    import time as _time
    if token not in SESSIONS or SESSIONS[token] < _time.time():
        await websocket.close(code=4001)
        return

    await manager.connect(websocket)
    try:
        while True:
            # Listen for messages from the browser (Buy/Skip decisions)
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "decision":
                    alert_id = msg.get("alert_id")
                    action = msg.get("action")  # "buy" | "skip"
                    if alert_id and action:
                        if action == "buy":
                            # S80 P6: real execution path. Routes through the
                            # shared manual_buy.execute_manual_buy helper that
                            # runs AutoExecutor.evaluate -> execute_trade(force=True)
                            # -> record_position. Ships the structured result
                            # back to the browser so the UI can show success
                            # OR failure rather than optimistically marking
                            # the alert as bought.
                            import sys
                            sys.path.insert(0, "/opt/projects/breadbot")
                            from manual_buy import execute_manual_buy
                            try:
                                result = await asyncio.to_thread(execute_manual_buy, alert_id)
                            except Exception as exc:
                                logger.error("manual_buy WS handler exception: %s", exc, exc_info=True)
                                await websocket.send_json({
                                    "type": "decision_ack",
                                    "alert_id": alert_id,
                                    "action": "buy",
                                    "success": False,
                                    "reason": "exception",
                                    "user_message": f"Buy handler crashed: {exc}",
                                })
                                continue

                            if result.success:
                                # Only NOW do we mark actioned — the trade is real.
                                manager.mark_actioned(alert_id)
                            await websocket.send_json({
                                "type": "decision_ack",
                                "alert_id": alert_id,
                                "action": "buy",
                                "success": result.success,
                                "reason": result.reason,
                                "user_message": result.user_message,
                                "decision_value": result.decision_value,
                                "position_id": result.position_id,
                            })
                        else:
                            # Skip path stays decision-only — no execution required.
                            manager.mark_actioned(alert_id)
                            from mcp_proxy import call_tool
                            await call_tool("record_decision", {"alert_id": alert_id, "action": "skip"})
                            await websocket.send_json({
                                "type": "decision_ack",
                                "alert_id": alert_id,
                                "action": "skip",
                                "success": True,
                            })
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
