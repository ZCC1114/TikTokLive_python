from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from live_service.manager import ConnectionManager
from live_service.upstream.bootstrap import normalize_username
from live_service.upstream.errors import UpstreamError

logger = logging.getLogger(__name__)


def create_app(manager: ConnectionManager | None = None) -> FastAPI:
    manager = manager or ConnectionManager()

    @asynccontextmanager
    async def lifespan(app):
        from live_service.log_config import setup_logging

        setup_logging()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(lifespan=lifespan)
    app.state.manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.websocket("/ws/{live_id}")
    async def websocket_endpoint(websocket: WebSocket, live_id: str) -> None:
        try:
            live_id = normalize_username(live_id)
        except ValueError:
            await websocket.close(code=1008)
            return
        try:
            await manager.connect(websocket, live_id)
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await manager.send(websocket, live_id, "pong")
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Front-end WebSocket failed: room=%s", live_id)
        finally:
            await manager.remove(websocket, live_id)

    @app.get("/internal/rooms/{username}")
    async def room_profile(username: str, x_live_service_key: str = Header(default="")):
        # Only the business service may perform room discovery. No credentials or
        # arbitrary upstream URLs cross this API, including in failure responses.
        configured_key = os.environ.get("LIVE_INTERNAL_API_KEY", "")
        if not configured_key or not hmac.compare_digest(configured_key, x_live_service_key):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            username = normalize_username(username)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_username") from None
        try:
            return await manager.resolve_room_profile(username)
        except UpstreamError as exc:
            code = 404 if exc.reason == "user_not_found" else 503
            raise HTTPException(status_code=code, detail=exc.reason) from None

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        status = manager.health()
        return JSONResponse(status, status_code=200 if status["ready"] else 503)

    return app
