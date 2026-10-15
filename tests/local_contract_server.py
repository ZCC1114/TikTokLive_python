"""Loopback-only upstream fixture for native App -> Java -> barrage integration.

The production ASGI application, room manager, enrichment and WebSocket wire
format run unchanged. Only TikTok itself is replaced by deterministic events.
Run from this repository with PYTHONPATH=.:tests and REDIS_PORT=63379.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import uvicorn
from conftest import Factory, comment
from fastapi import HTTPException
from pydantic import BaseModel

from live_service.app import create_app
from live_service.config import Settings
from live_service.manager import ConnectionManager

factory = Factory()
manager = ConnectionManager(Settings(), client_factory=factory)
manager.bootstrap.resolve = AsyncMock(side_effect=lambda username, **kwargs: SimpleNamespace(profile={
    "username": username, "roomId": "7300000000000000001", "live": True,
    "nickname": "Local contract creator", "avatarUrl": "https://example.invalid/avatar.png",
}))
app = create_app(manager)


@app.get("/__contract/config")
async def config():
    return json.loads(Path("/tmp/quick-pick-local-contract.json").read_text())


class Message(BaseModel):
    id: int
    content: str = "12"


@app.post("/__contract/comment")
async def inject(message: Message):
    active = [client for client in factory.clients if not client.closed]
    if not active:
        raise HTTPException(409, "No native subscriber is connected")
    for client in active:
        await client.event_sink(comment(message.id, message.content))
    return {"accepted": len(active)}


if __name__ == "__main__":
    if os.environ.get("LIVE_INTERNAL_API_KEY") != "quick-pick-local-contract-only":
        raise SystemExit("Set the local contract key; never load production credentials")
    uvicorn.run(app, host="127.0.0.1", port=63380, log_level="warning")
