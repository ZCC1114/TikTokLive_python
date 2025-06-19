import asyncio
import json
import uuid
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware

from TikTokLive.client.client import TikTokLiveClient
from TikTokLive.events import CommentEvent, ControlEvent, ConnectEvent
from TikTokLive.proto.custom_proto import ControlAction
from redis_helper import FsBlackRedisVo, TagUserVo, redis_client


app = FastAPI()


class ConnectionManager:
    """Manage front-end WebSocket connections and live clients.

    For each ``live_id`` only a single :class:`TikTokLiveClient` is created and
    its events are broadcast to all connected front ends.
    """

    def __init__(self) -> None:
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.clients: Dict[str, TikTokLiveClient] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def _run_client(self, live_id: str) -> None:
        """Start a TikTokLiveClient for ``live_id`` and forward comments.

        This coroutine runs in the background and broadcasts each comment to all
        currently connected WebSocket clients.
        """
        client = TikTokLiveClient(unique_id=live_id)
        self.clients[live_id] = client

        @client.on(ConnectEvent)
        async def on_open(_: ConnectEvent) -> None:
            print("\u3010\u221A\u3011WebSocket\u8fde\u63a5\u6210\u529f.")

        @client.on(ControlEvent)
        async def on_control(event: ControlEvent) -> None:
            await self.broadcast(live_id, str(event.action.value))
            if event.action == ControlAction.CONTROL_ACTION_STREAM_ENDED:
                print("\u76f4\u64ad\u95f4\u5df2\u7ed3\u675f")
                for ws in list(self.active_connections.get(live_id, [])):
                    await ws.close()
                    await self.remove(ws, live_id)
                await client.disconnect(close_client=True)

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent) -> None:
            message = {
                "msgId": str(uuid.uuid4()),
                "dyMsgId": str(event.base_message.message_id),
                # Create a background TikTokLiveClient only once per live_id
            # Track the newly connected front end
                    # No front-end connections left: stop the TikTokLiveClient
                "danmuUserId": str(event.user.unique_id),
                "danmuUserName": str(event.user.nick_name),
                "danmuContent": str(event.comment),
                "dyRoomId": str(event.base_message.room_id),
            }

            try:
                order_key = f"orderUser:dy_room_id_user:{message['dyRoomId']}:{message['danmuUserId']}"
                tag_user_str = redis_client.get(order_key)
                tag_user = TagUserVo.parse_from_redis(tag_user_str) if tag_user_str else None
                if tag_user:
                    message["orderNumber"] = tag_user.orderNumber or ""
                else:
                    message["orderNumber"] = ""

                black_str = redis_client.get(f"black:{message['danmuUserId']}")
                black_vo = FsBlackRedisVo.parse_from_redis(black_str) if black_str else None
                if black_vo:
                    message["blackLevel"] = str(black_vo.blackLevel)
                    message["createdUsers"] = black_vo.createdUsers
                else:
                    message["blackLevel"] = "0"
                    message["createdUsers"] = "[]"
            except Exception as e:
                print(f"\u274c 标签信息获取失败: {e}")

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