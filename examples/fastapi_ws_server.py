import asyncio
import json
import uuid
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware

from TikTokLive.client.client import TikTokLiveClient
from TikTokLive.events import CommentEvent


app = FastAPI()


class ConnectionManager:
    """Manage WebSocket clients and TikTokLive connections."""

    def __init__(self) -> None:
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.clients: Dict[str, TikTokLiveClient] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def _run_client(self, live_id: str) -> None:
        """Start TikTokLiveClient and forward comments to front-end."""
        client = TikTokLiveClient(unique_id=live_id)
        self.clients[live_id] = client

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent) -> None:
            message = {
                "msgId": str(uuid.uuid4()),
                "dyMsgId": str(event.base_message.message_id),
                "danmuUserId": str(event.user.unique_id),
                "danmuUserName": str(event.user.nick_name),
                "danmuContent": str(event.comment),
                "dyRoomId": str(event.base_message.room_id)
            }
            await self.broadcast(live_id, json.dumps(message, ensure_ascii=False))

        try:
            await client.start()
        except asyncio.CancelledError:
            pass
        finally:
            await client.disconnect(close_client=True)

    async def connect(self, websocket: WebSocket, live_id: str) -> None:
        await websocket.accept()
        async with self.lock:
            if live_id not in self.active_connections:
                self.active_connections[live_id] = set()
                if live_id not in self.clients:
                    self.tasks[live_id] = asyncio.create_task(self._run_client(live_id))
            self.active_connections[live_id].add(websocket)
        await websocket.send_text("LIVING")

    async def remove(self, websocket: WebSocket, live_id: str) -> None:
        async with self.lock:
            if live_id in self.active_connections:
                self.active_connections[live_id].discard(websocket)
                if not self.active_connections[live_id]:
                    if live_id in self.clients:
                        await self.clients[live_id].disconnect(close_client=True)
                    if live_id in self.tasks:
                        self.tasks[live_id].cancel()
                    self.active_connections.pop(live_id, None)
                    self.clients.pop(live_id, None)
                    self.tasks.pop(live_id, None)

    async def broadcast(self, live_id: str, text: str) -> None:
        clients = list(self.active_connections.get(live_id, []))
        for connection in clients:
            try:
                await connection.send_text(text)
            except Exception:
                await self.remove(connection, live_id)


manager = ConnectionManager()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/ws/{live_id}")
async def websocket_endpoint(websocket: WebSocket, live_id: str) -> None:
    await manager.connect(websocket, live_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await manager.remove(websocket, live_id)
    except Exception:
        await manager.remove(websocket, live_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)
