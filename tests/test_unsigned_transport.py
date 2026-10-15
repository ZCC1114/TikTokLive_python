import asyncio
import gzip
import json
import subprocess
import sys
import time
from dataclasses import replace
from http.cookiejar import Cookie, CookieJar
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import Enricher, Socket, comment, eventually
from websockets.legacy.server import serve

from live_service.manager import ConnectionManager
from live_service.protocol import comment_message
from live_service.upstream import bootstrap as http_bootstrap
from live_service.upstream.bootstrap import USER_AGENT, AnonymousBootstrap, BootstrapResult
from live_service.upstream.client import AnonymousLiveClient
from live_service.upstream.errors import UpstreamError
from live_service.upstream.protocol import WebcastResponse, connection_url, decode_frame
from TikTokLive.events import CommentEvent, ConnectEvent, ControlEvent
from TikTokLive.proto import ProtoMessageFetchResultBaseProtoMessage
from TikTokLive.proto.custom_extras import WebcastPushFrame

TOKEN = "unit-anonymous-cookie-no-real-credentials"
ROOM = 7300000000000000001


def anonymous_cookie():
    return Cookie(0, "ttwid", TOKEN, None, False, ".tiktok.com", True, True, "/", True, True,
                  int(time.time()) + 3600, False, None, None, {}, False)


class FakeHTTPSession:
    def __init__(self, *, code=200, register_cookie=True, room_status=2):
        self.cookies = SimpleNamespace(jar=CookieJar())
        self.calls = []
        self.closed = False
        self.code = code
        self.register_cookie = register_cookie
        self.room_status = room_status
        self.gate = None

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.gate:
            await self.gate.wait()
        await asyncio.sleep(0)
        if method == "POST":
            if self.register_cookie:
                self.cookies.jar.set_cookie(anonymous_cookie())
            payload = {"status_code": 0}
        else:
            payload = {"statusCode": 0, "data": {"user": {"roomId": str(ROOM), "status": self.room_status},
                                                 "liveRoom": {"status": self.room_status}}}
        kwargs["content_callback"](json.dumps(payload).encode())
        return SimpleNamespace(status_code=self.code, http_version=3, headers={"retry-after": "7"})

    async def close(self):
        self.closed = True


def credentials():
    return BootstrapResult(str(ROOM), f"ttwid={TOKEN}", USER_AGENT, {})


def response_frame(ids, *, history=False, compressed=True, control=None):
    messages = [ProtoMessageFetchResultBaseProtoMessage(
        method="WebcastChatMessage", msg_id=n, payload=bytes(comment(n)), is_history=history,
    ) for n in ids]
    if control is not None:
        messages.append(ProtoMessageFetchResultBaseProtoMessage(
            method="WebcastControlMessage", payload=bytes(ControlEvent(action=control)),
        ))
    payload = bytes(WebcastResponse(messages=messages, internal_ext=b"\xff\x00opaque-ack", needs_ack=True))
    return bytes(WebcastPushFrame(log_id=123, payload_type="msg", payload_encoding="gzip" if compressed else "pb",
                                  payload=gzip.compress(payload) if compressed else payload))


def test_service_import_and_default_factory_never_load_signer():
    script = """
import sys
from live_service.app import create_app
app = create_app()
assert app.state.manager._make_client('room').__class__.__name__ == 'AnonymousLiveClient'
assert not any(name.endswith(('web_signer','fetch_signed_websocket','client.client')) for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, timeout=10)


async def test_registration_single_flight_cache_expiry_and_close():
    session = FakeHTTPSession()
    provider = AnonymousBootstrap(session_factory=lambda _: session)
    try:
        results = await asyncio.gather(*(provider.resolve(f"room{i}") for i in range(8)))
        assert all(r.room_id == str(ROOM) and TOKEN in r.cookie for r in results)
        assert sum(method == "POST" for method, *_ in session.calls) == 1
        assert provider.room_lookups == 8
        before = len(session.calls)
        result = await provider.resolve("room", room_id=ROOM)
        assert len(session.calls) == before and result.diagnostics["room_cached"]
        provider._expires = time.monotonic() - 1
        await provider.resolve("room", room_id=ROOM)
        assert provider.registrations == 2
        assert TOKEN not in repr(result) and TOKEN not in json.dumps(result.diagnostics)
    finally:
        await provider.close()
    assert session.closed


@pytest.mark.parametrize("status,reason", [(403, "bootstrap_rejected"), (429, "rate_limited"), (302, "bootstrap_redirect_rejected")])
async def test_bootstrap_rejection_stops_without_retry_or_room_lookup(status, reason):
    session = FakeHTTPSession(code=status)
    provider = AnonymousBootstrap(session_factory=lambda _: session)
    try:
        with pytest.raises(UpstreamError) as caught:
            await provider.resolve("room")
        assert caught.value.reason == reason
        assert len(session.calls) == 1
        assert TOKEN not in str(caught.value)
        if status == 429:
            with pytest.raises(UpstreamError) as second:
                await provider.resolve("other")
            assert second.value.retry_after > 6 and len(session.calls) == 1
    finally:
        await provider.close()


async def test_bootstrap_cancellation_releases_single_flight_lock():
    session = FakeHTTPSession()
    session.gate = asyncio.Event()
    provider = AnonymousBootstrap(session_factory=lambda _: session)
    task = asyncio.create_task(provider.resolve("room"))
    try:
        await eventually(lambda: len(session.calls) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session.gate.set()
        assert (await provider.resolve("room")).room_id == str(ROOM)
    finally:
        await provider.close()


@pytest.mark.parametrize("register_cookie,room_status,reason", [(False, 2, "bootstrap_missing_cookie"), (True, 4, "offline")])
async def test_bootstrap_distinguishes_missing_cookie_and_offline(register_cookie, room_status, reason):
    session = FakeHTTPSession(register_cookie=register_cookie, room_status=room_status)
    provider = AnonymousBootstrap(session_factory=lambda _: session)
    try:
        with pytest.raises(UpstreamError) as caught:
            await provider.resolve("room")
        assert caught.value.reason == reason and caught.value.terminal
    finally:
        await provider.close()


def test_unsigned_url_has_no_signer_parameters_and_consistent_metadata():
    parts = urlsplit(connection_url(ROOM, 3))
    query = parse_qs(parts.query)
    assert parts.hostname == "webcast-ws.tiktok.com"
    assert not {"signature", "x-bogus", "x-gnarly", "x-signature"} & {k.lower() for k in query}
    assert query["room_id"] == [str(ROOM)]
    assert query["browser_version"] == [USER_AGENT.removeprefix("Mozilla/")]


@pytest.mark.parametrize("compressed", [False, True])
async def test_real_websocket_delivers_contract_ack_and_lifecycle(settings, compressed):
    received, server_errors, frames = [], [], []
    complete = asyncio.Event()

    async def upstream(ws):
        try:
            for _ in range(2):
                frames.append(WebcastPushFrame().parse(await ws.recv()).payload_type)
            for ids, action in (([2**53 + 77], 1), ([2**53 + 78], 2), ([], 3)):
                await ws.send(response_frame(ids, compressed=compressed, control=action))
                item = WebcastPushFrame().parse(await ws.recv())
                assert item.payload_type == "ack" and item.log_id == 123 and item.payload == b"\xff\x00opaque-ack"
            complete.set()
            await ws.wait_closed()
        except Exception as exc:
            server_errors.append(exc)

    provider = SimpleNamespace(resolve=AsyncMock(return_value=credentials()))
    async with serve(upstream, "127.0.0.1", 0, ping_interval=None) as server:
        client = AnonymousLiveClient("room", provider, settings, ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        client.event_sink = AsyncMock(side_effect=lambda event: received.append(event))
        try:
            runtime = await client.start()
            await asyncio.wait_for(runtime, 2)
            assert complete.is_set() and not server_errors
            assert frames == ["im_enter_room", "hb"]
            assert isinstance(received[0], ConnectEvent)
            comments = [comment_message(e) for e in received if isinstance(e, CommentEvent)]
            assert [c["dyMsgId"] for c in comments] == [str(2**53 + 77), str(2**53 + 78)]
            assert comments[0]["username"] == comments[0]["danmuUserId"] == "viewer"
            assert comments[0]["danmuUserName"] == "观众" and comments[0]["danmuContent"] == "你好 🌷"
            assert [e.action for e in received if type(e) is ControlEvent] == [1, 2, 3]
            assert not client.connected
        finally:
            await client.disconnect()


async def test_silent_open_socket_times_out_without_waiting_for_tcp_close(settings):
    async def blackhole(ws):
        async for _ in ws:
            pass  # Read client heartbeats, deliberately never send anything.

    settings = replace(settings, upstream_heartbeat_interval=0.02, upstream_idle_timeout=0.12)
    provider = SimpleNamespace(resolve=AsyncMock(return_value=credentials()))
    async with serve(blackhole, "127.0.0.1", 0, ping_interval=None) as server:
        client = AnonymousLiveClient("room", provider, settings, ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        client.event_sink = AsyncMock()
        try:
            runtime = await client.start()
            started = time.monotonic()
            with pytest.raises(UpstreamError, match="upstream_idle_timeout"):
                await asyncio.wait_for(runtime, 1)
            assert time.monotonic() - started < 0.5 and not client.connected
        finally:
            await client.disconnect()


async def test_stalled_handshake_cancellation_does_not_emit_connected_or_leak_socket(settings):
    entered, closed = asyncio.Event(), asyncio.Event()

    async def stalled(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(stalled, "127.0.0.1", 0)
    async with server:
        provider = SimpleNamespace(resolve=AsyncMock(return_value=credentials()))
        client = AnonymousLiveClient("room", provider, settings, ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        client.event_sink = AsyncMock()
        task = asyncio.create_task(client.start())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(closed.wait(), 1)
            client.event_sink.assert_not_awaited()
        finally:
            await client.disconnect()


async def test_wire_reconnect_preserves_existing_subscriber_dedup_and_shared_registration(settings):
    attempts = 0
    async def upstream(ws):
        nonlocal attempts
        attempts += 1
        this_attempt = attempts
        await ws.recv()
        await ws.recv()
        await ws.send(response_frame([7, 7] if this_attempt == 1 else [7, 8, 8], history=this_attempt > 1))
        await ws.recv()  # ACK
        if this_attempt == 1:
            ws.transport.abort()
        else:
            async for _ in ws:
                pass

    provider = SimpleNamespace(resolve=AsyncMock(return_value=credentials()))
    async with serve(upstream, "127.0.0.1", 0, ping_interval=None) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        clients = []
        def factory(key):
            client = AnonymousLiveClient(key, provider, settings, ws_url=url)
            clients.append(client)
            return client
        manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
        peers = [Socket(), Socket()]
        try:
            await asyncio.gather(*(manager.connect(peer, "room") for peer in peers))
            await eventually(lambda: all(sum(m.startswith('{') for m in p.messages) == 2 for p in peers))
            assert attempts == 2 and len(clients) == 2
            for peer in peers:
                assert [json.loads(m)["dyMsgId"] for m in peer.messages if m.startswith('{')] == ["7", "8"]
            assert provider.resolve.call_args.kwargs["room_id"] == ROOM
            assert manager.health()["suppressed_replays"] == 2
        finally:
            await manager.close()


def test_compression_bomb_rejected_before_large_allocation(monkeypatch):
    from live_service.upstream import protocol
    monkeypatch.setattr(protocol, "MAX_PAYLOAD", 4096)
    data = bytes(WebcastPushFrame(payload_type="msg", payload=gzip.compress(b"x" * 100_000)))
    with pytest.raises(UpstreamError, match="payload_too_large"):
        decode_frame(data)


async def test_async_http_large_response_aborts_and_releases_handle():
    done = asyncio.Event()
    async def endpoint(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4194304\r\nConnection: close\r\n\r\n")
            for _ in range(256):
                writer.write(b"x" * 16384)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            done.set()
    server = await asyncio.start_server(endpoint, "127.0.0.1", 0)
    session = http_bootstrap._new_session(2)
    async with server:
        try:
            with pytest.raises(http_bootstrap.PirateTokBootstrapError, match="body_too_large"):
                await http_bootstrap._request(session, "GET", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                               "bounded-test", time.monotonic() + 2, {"stages": []})
        finally:
            await session.close()
            await asyncio.wait_for(done.wait(), 2)

@pytest.mark.parametrize('value', ['https://www.tiktok.com', 'https://www.tiktok.com/', '@', 'invalid name'])
def test_invalid_room_input_is_rejected_cleanly(value):
    with pytest.raises(ValueError):
        http_bootstrap.normalize_username(value)


async def test_bootstrap_total_deadline_includes_shared_lock_wait():
    session = FakeHTTPSession()
    session.gate = asyncio.Event()
    bootstrap = AnonymousBootstrap(timeout=0.06, session_factory=lambda _: session)
    first = asyncio.create_task(bootstrap.resolve('room'))
    await eventually(lambda: len(session.calls) == 1)
    second = asyncio.create_task(bootstrap.resolve('room2'))
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, UpstreamError) and result.reason == 'bootstrap_timeout' for result in results)
    session.gate.set()
    assert (await bootstrap.resolve('room')).room_id == str(ROOM)
    await bootstrap.close()


async def test_shared_bootstrap_concurrent_close_only_closes_pool_once():
    session = FakeHTTPSession()
    session.close = AsyncMock(side_effect=lambda: asyncio.sleep(0))
    bootstrap = AnonymousBootstrap(session_factory=lambda _: session)
    await bootstrap.resolve('room')
    # Use a coroutine side effect so AsyncMock doesn't leave an unawaited sleep.
    async def close_pool():
        await asyncio.sleep(0.01)
    session.close.side_effect = close_pool
    await asyncio.gather(bootstrap.close(), bootstrap.close())
    session.close.assert_awaited_once()
    with pytest.raises(UpstreamError, match='bootstrap_closed'):
        await bootstrap.resolve('room')


def test_http_date_retry_after_respects_server_cooldown(monkeypatch):
    from live_service.upstream import errors
    monkeypatch.setattr(errors.time, 'time', lambda: 0)
    assert errors.retry_after_seconds('Thu, 01 Jan 1970 00:02:00 GMT') == 120
    assert errors.retry_after_seconds(' 7 ') == 7
    assert errors.retry_after_seconds('-1') == 60
    assert errors.retry_after_seconds('not a date') == 60
