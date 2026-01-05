import asyncio
import json
import uuid
from typing import Dict, Set
import contextlib
import os

import httpx

from log_config import setup_logging
import logging

setup_logging()
logger = logging.getLogger(__name__)


from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware

from TikTokLive.client.client import TikTokLiveClient
from TikTokLive.events import CommentEvent, ControlEvent, ConnectEvent
from TikTokLive.proto.custom_proto import ControlAction
from TikTokLive.client.web.web_settings import WebDefaults
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
        # 记录每个 live_id 的“是否已经连上 TikTok”
        self.live_connected: Dict[str, bool] = {}

    async def _run_client(self, live_id: str) -> None:
        """Start a TikTokLiveClient for ``live_id`` and forward comments.

        This coroutine runs in the background and broadcasts each comment to all
        currently connected WebSocket clients.
        """
        api_key = os.getenv("EULERSTREAM_API_KEY", "")
        if api_key:
            WebDefaults.tiktok_sign_api_key = api_key
            logger.info(f"当前 WebDefaults.tiktok_sign_api_key = {WebDefaults.tiktok_sign_api_key[:8]}****")
        else:
            logger.warning("环境变量 EULERSTREAM_API_KEY 为空，将使用默认限流配置")


        client = TikTokLiveClient(unique_id=live_id)
        self.clients[live_id] = client
        logger.info(f"\U0001f7e2 Start TikTokLiveClient for {live_id}")

        @client.on(ConnectEvent)
        async def on_open(_: ConnectEvent) -> None:
            logger.info("\u3010\u221a\u3011WebSocket\u8fde\u63a5\u6210\u529f.")
            # 标记这个直播间已经连上 TikTok
            async with self.lock:
                self.live_connected[live_id] = True
                # 拿一份当前所有连接的快照，避免在锁里 await
                targets = list(self.active_connections.get(live_id, []))

            # 给当前所有已连接的前端发一次 LIVING（第一次连上时）
            for ws in targets:
                try:
                    await ws.send_text("LIVING")
                except Exception:
                    # 某个连接挂了就算了，不影响别人
                    pass

        @client.on(ControlEvent)
        async def on_control(event: ControlEvent) -> None:
            # 1. 明确状态码映射，推送数字字符串
            if event.action == ControlAction.CONTROL_ACTION_STREAM_ENDED:
                status = 3
            elif event.action == ControlAction.CONTROL_ACTION_STREAM_PAUSED:
                status = 1
            elif event.action == ControlAction.CONTROL_ACTION_STREAM_UNPAUSED:
                status = 2
            else:
                status = 0

            # 2. 推送数字字符串（和 Java、抖音完全一致）
            await self.broadcast(live_id, str(status))

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent) -> None:
            comment_id = str(event.base_message.message_id)

            message = {
                "msgId": str(uuid.uuid4()),
                "dyMsgId": comment_id,
                "danmuUserId": str(event.user.unique_id),
                "danmuUserName": str(event.user.nick_name),
                "danmuContent": str(event.comment),
                "dyRoomId": str(event.base_message.room_id),
                "fansStatus": str(0)
            }

            try:
                order_key = f"orderUser:dy_room_id_user:{message['dyRoomId']}:{message['danmuUserId']}"
                tag_user_str = redis_client.get(order_key)
                tag_user = (
                    TagUserVo.parse_from_redis(tag_user_str) if tag_user_str else None
                )
                if tag_user:
                    message["orderNumber"] = tag_user.orderNumber or ""
                else:
                    message["orderNumber"] = ""

                black_str = redis_client.get(f"black:{message['danmuUserId']}")
                black_vo = (
                    FsBlackRedisVo.parse_from_redis(black_str) if black_str else None
                )
                if black_vo:
                    message["blackLevel"] = str(black_vo.blackLevel)
                    message["createdUsers"] = black_vo.createdUsers
                else:
                    message["blackLevel"] = "0"
                    message["createdUsers"] = "[]"
            except Exception as e:
                logger.error(f"\u274c 标签信息获取失败: {e}")

            await self.broadcast(live_id, json.dumps(message, ensure_ascii=False))

        try:
            # 启动 TikTokLiveClient（这里里面会去请求 EulerStream）
            await client.start()

        except asyncio.CancelledError:
            # 正常取消（比如前端都断开了），不算错误
            logger.info(f"TikTokLiveClient task cancelled for {live_id}")
        except httpx.ReadTimeout:
            # 签名服务超时：后端记一条 warning，并告诉前端“超时”
            logger.warning(f"Sign API ReadTimeout，停止本次客户端: {live_id}")
            await self.broadcast(live_id, "SIGN_API_TIMEOUT")
        except Exception:
            # 其他未知异常：记录栈信息，并告诉前端“连接失败”
            logger.exception(f"TikTokLiveClient 运行异常: {live_id}")
            await self.broadcast(live_id, "LIVE_CONNECT_ERROR")
        finally:
            # 无论如何都做资源清理
            try:
                await client.disconnect(close_client=True)
            except Exception as e:
                # 这里很容易重复关闭，所以用 warning 或直接忽略
                logger.warning(f"Disconnect 时出错（可能已经断开，无需处理）: {e}")
            async with self.lock:
                if self.clients.get(live_id) is client:
                    self.clients.pop(live_id, None)
                if self.tasks.get(live_id) is asyncio.current_task():
                    self.tasks.pop(live_id, None)
            logger.info(f"🔴 TikTokLiveClient closed for {live_id}")

    async def connect(self, websocket: WebSocket, live_id: str) -> None:
        # 1. 接受前端 WebSocket 连接
        await websocket.accept()
        logger.info(f"前端连接接入: live_id={live_id}")

        already_connected = False

        async with self.lock:
            # 2. 维护当前直播间的前端连接集合
            if live_id not in self.active_connections:
                self.active_connections[live_id] = set()
            self.active_connections[live_id].add(websocket)
            logger.info(
                f"live_id={live_id} 前端连接数={len(self.active_connections[live_id])}"
            )

            already_connected = self.live_connected.get(live_id, False)

            # 3. 如果这个直播间还没有对应的 TikTokLiveClient，就创建一个
            if live_id not in self.clients:
                logger.info(f"为 {live_id} 创建新的 TikTokLiveClient")
                task = asyncio.create_task(self._run_client(live_id))
                self.tasks[live_id] = task
            else:
                logger.info(
                    f"{live_id} 已有 TikTokLiveClient，复用现有 client，"
                    f"当前前端连接数 = {len(self.active_connections[live_id])}"
                )

        try:
            if already_connected:
                # 如果 TikTok 那边已经连上了，新的前端直接收到 LIVING
                await websocket.send_text("LIVING")
                logger.info(f"live_id={live_id} 初始状态=LIVING")
            else:
                # 如果 TikTok 还没连上，让前端先显示“连接中”
                await websocket.send_text("CONNECTING")
                logger.info(f"live_id={live_id} 初始状态=CONNECTING")
        except Exception:
            # 如果刚 accept 完就发不出去，也不要让它影响后面的逻辑
            logger.warning(f"给 {live_id} 发送 CONNECTING 失败，可能前端已断开")



    async def remove(self, websocket: WebSocket, live_id: str) -> None:
        stop_task = None
        stop_client = None
        async with self.lock:
            if live_id in self.active_connections:
                self.active_connections[live_id].discard(websocket)
                if not self.active_connections[live_id]:
                    stop_client = self.clients.pop(live_id, None)
                    stop_task = self.tasks.pop(live_id, None)
                    self.active_connections.pop(live_id, None)
                    self.live_connected.pop(live_id, None)
                    logger.info(f"live_id={live_id} 无前端连接，准备关闭 TikTokLiveClient")

        if stop_client:
            logger.info(f"\U0001f534 Stop TikTokLiveClient for {live_id}")
            try:
                await stop_client.disconnect(close_client=True)
            except Exception as e:
                logger.error(f"Disconnect error: {e}")
        if stop_task:
            stop_task.cancel()
            with contextlib.suppress(BaseException):
                await stop_task

    async def broadcast(self, live_id: str, text: str) -> None:
        clients = list(self.active_connections.get(live_id, []))
        for connection in clients:
            try:
                await connection.send_text(text)
            except Exception:
                logger.warning(f"live_id={live_id} 广播失败，移除连接")
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
        logger.info(f"前端客户端断开: live_id={live_id}")
        await manager.remove(websocket, live_id)
    except Exception:
        logger.exception(f"前端连接异常: live_id={live_id}")
        await manager.remove(websocket, live_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)
