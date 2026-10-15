import asyncio
import gzip
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import redis.asyncio as redis
from conftest import Enricher, Factory, comment, eventually
from fastapi.testclient import TestClient
from websockets.legacy.server import serve

from live_service.app import create_app
from live_service.enrichment import RedisEnricher
from live_service.manager import ConnectionManager
from live_service.protocol import comment_message
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent
from TikTokLive.proto import (
    ProtoMessageFetchResult,
    ProtoMessageFetchResultBaseProtoMessage,
)
from TikTokLive.proto.custom_extras import (
    HeartbeatMessage,
    WebcastImEnterRoomMessage,
    WebcastPushFrame,
)


def test_legacy_endpoint_ping_comment_and_lifespan(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    factory, enricher = Factory(), Enricher()
    manager = ConnectionManager(settings, client_factory=factory, enricher=enricher)
    app = create_app(manager)
    with TestClient(app) as browser:
        with browser.websocket_connect("/ws/room") as socket:
            assert socket.receive_text() == "CONNECTING"
            assert socket.receive_text() == "LIVING"
            socket.send_text("ping")
            assert socket.receive_text() == "pong"
            browser.portal.call(factory.clients[0].event_sink, comment(5))
            message = socket.receive_json()
            assert message["dyMsgId"] == "5"
            assert message["danmuContent"] == "你好 🌷"
            assert message["createdUsers"] == []
            assert browser.get("/healthz").json() == {"status": "ok"}
            assert browser.get("/readyz").json()["connected_rooms"] == 1
    assert enricher.closed
    assert all(client.closed for client in factory.clients)


async def test_real_websocket_continuous_frames_enter_heartbeat_and_ack():
    received = []
    server_errors = []

    async def upstream(socket):
        try:
            entry = WebcastPushFrame().parse(await asyncio.wait_for(socket.recv(), 2))
            assert entry.payload_type == "im_enter_room"
            assert WebcastImEnterRoomMessage().parse(entry.payload).room_id == 7
            heartbeat = WebcastPushFrame().parse(await asyncio.wait_for(socket.recv(), 2))
            assert heartbeat.payload_type == "hb"
            assert HeartbeatMessage().parse(heartbeat.payload).send_packet_seq_id == 1
            # This isn't a fetch-result protobuf. It must not break decoding.
            await socket.send(bytes(WebcastPushFrame(payload_type="hb", payload=b"\xff")))
            for number in (1, 2, 3):
                response = ProtoMessageFetchResult(
                    messages=[
                        ProtoMessageFetchResultBaseProtoMessage(
                            method="WebcastChatMessage",
                            msg_id=number,
                            payload=bytes(comment(number)),
                        )
                    ],
                    need_ack=True,
                    internal_ext="receipt",
                )
                await socket.send(
                    bytes(
                        WebcastPushFrame(
                            log_id=number,
                            payload_type="msg",
                            headers={"compress_type": "gzip"},
                            payload=gzip.compress(bytes(response)),
                        )
                    )
                )
                ack = WebcastPushFrame().parse(await asyncio.wait_for(socket.recv(), 2))
                assert ack.payload_type == "ack" and ack.log_id == number and ack.payload == b"receipt"
            await socket.close()
        except Exception as exc:
            server_errors.append(exc)
            raise

    async with serve(upstream, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        client = TikTokLiveClient(unique_id="test", ws_kwargs={"uri": f"ws://127.0.0.1:{port}", "tcp_keepalive": True})
        client.web.fetch_room_id_from_html = AsyncMock(return_value=7)
        client.web.fetch_is_live = AsyncMock(return_value=True)
        client.web.fetch_signed_websocket = AsyncMock(return_value=ProtoMessageFetchResult(is_first=False))

        async def sink(event):
            if isinstance(event, CommentEvent):
                received.append(event.base_message.message_id)

        client.event_sink = sink
        try:
            runtime = await client.start()
            await asyncio.wait_for(runtime, 5)
            assert received == [1, 2, 3]
            assert not server_errors
            assert client._ws._ping_loop is None
            assert client._ws._connection_generator is None
        finally:
            await client.disconnect(close_client=True)


async def test_heartbeat_send_failure_wakes_reader_and_propagates():
    from TikTokLive.client.ws.ws_client import WebcastWSClient

    async def upstream(socket):
        await socket.wait_closed()

    async with serve(upstream, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        client = WebcastWSClient(ws_kwargs={"uri": f"ws://127.0.0.1:{port}"})
        real_send = client.send

        async def fail_heartbeat(message):
            if message.payload_type == "hb":
                raise OSError("heartbeat injection")
            await real_send(message)

        client.send = fail_heartbeat

        async def read():
            async for _ in client.connect(7, httpx.Cookies(), "test", ProtoMessageFetchResult()):
                pass

        with pytest.raises(OSError, match="heartbeat injection"):
            await asyncio.wait_for(read(), 3)
        assert client._ping_loop is None and client._connection_generator is None


async def test_real_redis_pipeline_and_wrongtype_compatibility():
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server is required for this local integration test")
    with tempfile.TemporaryDirectory(prefix="ttredis-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "redis.sock")
        process = subprocess.Popen(
            [
                binary,
                "--port",
                "0",
                "--unixsocket",
                socket_path,
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                directory,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = redis.Redis(unix_socket_path=socket_path, decode_responses=True)
        try:
            await eventually(lambda: Path(socket_path).exists(), timeout=3)
            assert await client.ping()
            message = comment_message(comment())
            tag_key = f"orderUser:dy_room_id_user:{message['dyRoomId']}:viewer"
            await client.set(tag_key, json.dumps(dict(id="1", orderNameId="viewer", orderNumber="A007", orderAmounts="3")))
            await client.set(
                "black:viewer", json.dumps(dict(orderNameId="viewer", blackLevel=3, createdUsers=["alice"]))
            )
            enricher = RedisEnricher(timeout=1, cooldown=0, client=client)
            await client.set("quick_pick:metadata_ready:v1", "1")
            await enricher.enrich(message)
            assert message["orderNumber"] == "A007" and message["createdUsers"] == ["alice"]
            await client.delete("black:viewer")
            await client.lpush("black:viewer", "wrong type")
            failed = comment_message(comment(2))
            await enricher.enrich(failed)
            assert failed["metadataAvailable"] is False
            assert "orderNumber" not in failed
            assert "blackLevel" not in failed and "createdUsers" not in failed
            await client.flushdb()  # This test owns this isolated Unix-socket Redis process.
            lost = comment_message(comment(3))
            await enricher.enrich(lost)
            assert lost["metadataAvailable"] is False
            await client.set("quick_pick:metadata_ready:v1", "1")
            fresh = comment_message(comment(4))
            await enricher.enrich(fresh)
            assert fresh["metadataAvailable"] is True and fresh["blackLevel"] == "0"
        finally:
            await client.aclose()
            process.terminate()
            await asyncio.to_thread(process.wait, 3)
