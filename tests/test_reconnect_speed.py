import asyncio
import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from conftest import comment
from websockets.legacy.server import serve

from TikTokLive import TikTokLiveClient
from TikTokLive.client.ws.ws_connect import WebcastConnect
from TikTokLive.events import CommentEvent, WebsocketResponseEvent
from TikTokLive.proto import ProtoMessageFetchResult, ProtoMessageFetchResultBaseProtoMessage
from TikTokLive.proto.custom_extras import WebcastPushFrame


async def test_fast_start_waits_for_real_handshake_without_resolving_known_room():
    entered = asyncio.Event()

    async def upstream(ws):
        frame = WebcastPushFrame().parse(await ws.recv())
        assert frame.payload_type == "im_enter_room"
        entered.set()
        await ws.wait_closed()

    async with serve(upstream, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        client = TikTokLiveClient(unique_id="test", ws_kwargs={"uri": f"ws://127.0.0.1:{port}"})
        client.web.fetch_room_id_from_html = AsyncMock(side_effect=AssertionError("unexpected scrape"))
        client.web.fetch_is_live = AsyncMock(side_effect=AssertionError("unexpected live check"))
        client.web.fetch_signed_websocket = AsyncMock(return_value=ProtoMessageFetchResult())
        try:
            reader = await asyncio.wait_for(client.start(
                room_id=7, fetch_live_check=False, wait_connected=True,
                sign_api_timeout=4.0, sign_api_retries=0,
            ), 2)
            await asyncio.wait_for(entered.wait(), 1)
            assert client.connected and not reader.done()
            assert client.connection_phase == "connected"
            assert client.connect_timings["resolve_room_seconds"] == 0
            assert client.connect_timings["live_check_seconds"] == 0
            assert 0 < client.connect_timings["handshake_seconds"] <= client.connect_timings["total_seconds"]
            client.web.fetch_signed_websocket.assert_awaited_once_with(
                preferred_agent_ids=None, timeout_seconds=4.0, retries=0,
            )
        finally:
            await client.disconnect(close_client=True)


async def test_start_deadline_includes_stalled_handshake_and_releases_transport():
    upgrade_requested = asyncio.Event()
    peer_closed = asyncio.Event()

    async def stalled_upgrade(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            upgrade_requested.set()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()

    server = await asyncio.start_server(stalled_upgrade, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        client = TikTokLiveClient(unique_id="test", ws_kwargs={
            "uri": f"ws://127.0.0.1:{port}", "close_timeout": 0.01,
        })
        client.web.fetch_signed_websocket = AsyncMock(return_value=ProtoMessageFetchResult())
        starting = asyncio.create_task(client.start(room_id=7, fetch_live_check=False, wait_connected=True))
        try:
            await asyncio.wait_for(upgrade_requested.wait(), 1)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(starting, 0.03)
            await asyncio.wait_for(peer_closed.wait(), 1)
            assert client._event_loop_task.done()
            assert client._ws._connection_generator is None
            assert not client._starting
            assert client.connection_phase == "handshake"
            assert client.connect_timings["handshake_seconds"] > 0
        finally:
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
            await client.disconnect(close_client=True)


async def test_wait_connected_propagates_transport_error_before_ready():
    client = TikTokLiveClient(unique_id="test")
    client.web.fetch_signed_websocket = AsyncMock(return_value=ProtoMessageFetchResult())

    async def responses(**kwargs):
        raise ConnectionResetError("handshake reset")
        yield  # make this a failing async generator

    client._ws.connect = responses
    try:
        with pytest.raises(ConnectionResetError, match="handshake reset"):
            await asyncio.wait_for(client.start(room_id=7, fetch_live_check=False, wait_connected=True), 1)
        assert client._event_loop_task.done()
    finally:
        await client.disconnect(close_client=True)


async def test_supervisor_can_disable_signer_inner_retries(monkeypatch):
    client = TikTokLiveClient(unique_id="test")
    monkeypatch.setenv("SIGN_API_RETRIES", "2")
    client.web.signer.client.get = AsyncMock(side_effect=httpx.ReadTimeout("injected"))
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.start(room_id=7, fetch_live_check=False, sign_api_retries=0, sign_api_timeout=0.1)
        assert client.web.signer.client.get.await_count == 1
        assert client.web.signer.client.get.call_args.kwargs["timeout"] == 0.1
        assert client.connection_phase == "sign"
        assert client.connect_timings["sign_seconds"] > 0
    finally:
        await client.close()


async def test_service_skips_raw_envelope_copy_but_explicit_listener_preserved(monkeypatch):
    client = TikTokLiveClient(unique_id="test")
    client.process_raw_events = False
    message = ProtoMessageFetchResultBaseProtoMessage(method="WebcastChatMessage", payload=bytes(comment(3)))
    # The real reader has parsed the enclosing fetch-result before its events.
    ProtoMessageFetchResult(messages=[message])
    raw_copy = Mock(wraps=WebsocketResponseEvent().from_dict)
    monkeypatch.setattr(WebsocketResponseEvent, "from_dict", raw_copy)
    try:
        events = await client._parse_webcast_response_message(message)
        assert any(type(event) is CommentEvent for event in events)
        raw_copy.assert_not_called()
        client.on(WebsocketResponseEvent, lambda event: None)
        events = await client._parse_webcast_response_message(message)
        raw_copy.assert_called_once()
        assert any(type(event) is WebsocketResponseEvent for event in events)
    finally:
        await client.close()


def test_tcp_failure_detection_options_are_configurable_without_ws_pongs():
    connection = WebcastConnect(
        ProtoMessageFetchResult(), logging.getLogger("test"), {}, "", uri="ws://127.0.0.1:1",
        tcp_keepalive=True, tcp_keepidle=5, tcp_keepintvl=2, tcp_keepcnt=3, tcp_user_timeout=10000,
    )
    sock = Mock()
    connection._configure_keepalive(SimpleNamespace(transport=SimpleNamespace(get_extra_info=lambda _: sock)))
    sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3), ("TCP_USER_TIMEOUT", 10000)):
        option = getattr(socket, name, None)
        if option is not None:
            sock.setsockopt.assert_any_call(socket.IPPROTO_TCP, option, value)
    idle = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
    if idle is not None:
        sock.setsockopt.assert_any_call(socket.IPPROTO_TCP, idle, 5)


@pytest.mark.parametrize("kwargs", [{"tcp_keepidle": 0}, {"tcp_keepcnt": -1}, {"tcp_user_timeout": float("nan")}])
def test_invalid_tcp_detection_options_fail_before_opening_socket(kwargs):
    with pytest.raises(ValueError):
        WebcastConnect(ProtoMessageFetchResult(), logging.getLogger("test"), {}, "", uri="ws://127.0.0.1:1", **kwargs)
