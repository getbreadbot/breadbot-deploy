"""
Breadbot Web Control Panel — FastAPI Backend
Serves the React frontend and proxies all bot operations through MCP.
"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from auth import router as auth_router, verify_session
from mcp_proxy import router as mcp_router
from railway_api import router as railway_router
from websocket_manager import manager, router as ws_router

FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background task that polls bot for alerts and fans out to connected browsers
    task = asyncio.create_task(manager.poll_bot_alerts())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Breadbot Panel", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes — all under /api prefix
app.include_router(auth_router,    prefix="/api/auth",    tags=["auth"])
app.include_router(mcp_router,     prefix="/api/bot",     tags=["bot"])
app.include_router(railway_router, prefix="/api/settings", tags=["settings"])
app.include_router(ws_router,      prefix="/api/ws",      tags=["websocket"])

@app.get("/api/health")
async def health():
    return {"status": "ok"}

# Serve React build — must come after API routes
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str, request: Request):
        # Let /api routes fall through to their handlers
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        index = FRONTEND_DIR / "index.html"
        return FileResponse(index)
else:
    @app.get("/")
    async def no_frontend():
        return {"status": "backend running — frontend not built yet"}



