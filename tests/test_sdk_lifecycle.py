import asyncio
import json
import time
from email.utils import formatdate
from unittest.mock import AsyncMock

import httpx
import pytest

from TikTokLive import TikTokLiveClient
from TikTokLive.client.errors import (
    AlreadyConnectedError,
    SignatureRateLimitError,
    UserOfflineError,
)
from TikTokLive.client.web.routes.fetch_room_id_live_html import (
    FetchRoomIdLiveHTMLRoute,
)
from TikTokLive.client.ws.ws_client import WebcastWSClient
from TikTokLive.events import ControlEvent, DisconnectEvent, LiveUnpauseEvent
from TikTokLive.proto import (
    ProtoMessageFetchResult,
    ProtoMessageFetchResultBaseProtoMessage,
)
from TikTokLive.proto.custom_proto import ControlAction


async def test_cancelled_reader_still_closes_all_http_pools():
    client = TikTokLiveClient(unique_id="offline")
    client._event_loop_task = asyncio.get_running_loop().create_future()
    client._event_loop_task.cancel()
    await client.disconnect(close_client=True)
    assert client.web.httpx_client.is_closed
    assert client.web.signer.client.is_closed
    assert client._event_loop_task is None
    await client.disconnect(close_client=True)


async def test_main_http_close_failure_does_not_skip_signer_close():
    client = TikTokLiveClient(unique_id="offline")
    actual_close = client.web.httpx_client.aclose
    client.web.httpx_client.aclose = AsyncMock(side_effect=OSError("close failed"))
    try:
        with pytest.raises(OSError):
            await client.close()
        assert client.web.signer.client.is_closed
    finally:
        await actual_close()


async def test_start_is_single_flight_and_cancellation_resets_guard():
    client = TikTokLiveClient(unique_id="offline")
    gate = asyncio.Event()

    async def resolve(*args):
        await gate.wait()
        return 1

    client.web.fetch_room_id_from_html = resolve
    first = asyncio.create_task(client.start())
    try:
        await asyncio.sleep(0)
        with pytest.raises(AlreadyConnectedError):
            await client.start()
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert not client._starting
        await client.disconnect(close_client=True)


async def test_reader_exception_emits_disconnect_and_closes_generator():
    client = TikTokLiveClient(unique_id="offline")
    closed, disconnected = asyncio.Event(), []

    async def responses(**kwargs):
        try:
            yield ProtoMessageFetchResult(is_first=True)
            raise ConnectionResetError("injected")
        finally:
            closed.set()

    client._ws.connect = responses
    client.on(DisconnectEvent, lambda event: disconnected.append(event))
    try:
        with pytest.raises(ConnectionResetError):
            await client._ws_client_loop(ProtoMessageFetchResult(), True, True)
        assert closed.is_set() and len(disconnected) == 1
    finally:
        await client.close()


async def test_unpause_custom_event_is_reachable():
    client = TikTokLiveClient(unique_id="offline")
    control = ControlEvent(action=ControlAction.CONTROL_ACTION_STREAM_UNPAUSED)
    try:
        result = await client.handle_custom_event(
            ProtoMessageFetchResultBaseProtoMessage(payload=bytes(control)), control
        )
        assert isinstance(result, LiveUnpauseEvent)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "script,data",
    [
        ("SIGI_STATE", {"LiveRoom": {"liveRoomUserInfo": {"user": {"status": 4, "roomId": "1"}}}}),
        (
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
            {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {"user": {"status": 4, "roomId": "1"}}}}},
        ),
    ],
)
def test_offline_status_survives_html_parsing(script, data):
    html = f'<script id="{script}" type="application/json">{json.dumps(data)}</script>'
    with pytest.raises(UserOfflineError):
        FetchRoomIdLiveHTMLRoute.parse_room_id(html)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({}, 60),
        ({"RateLimit-Remaining": "0"}, 60),
        ({"Retry-After": "7"}, 7),
        ({"Retry-After": "2.3"}, 3),
        ({"Retry-After": "invalid", "RateLimit-Reset": "bad"}, 60),
        ({"Retry-After": "nan"}, 60),
    ],
)
def test_rate_limit_missing_or_invalid_headers_are_safe(headers, expected):
    assert SignatureRateLimitError.calculate_retry_after(httpx.Response(429, headers=headers)) == expected


def test_rate_limit_http_date_and_absolute_reset():
    for headers in (
        {"Retry-After": formatdate(time.time() + 60, usegmt=True)},
        {"RateLimit-Reset": str(time.time() + 60)},
    ):
        delay = SignatureRateLimitError.calculate_retry_after(httpx.Response(429, headers=headers))
        assert 59 <= delay <= 60


def test_websocket_default_constructor_and_invalid_send_timeout():
    assert WebcastWSClient().connected is False
    with pytest.raises(ValueError):
        WebcastWSClient(ws_kwargs={"send_timeout": float("nan")})


async def test_room_resolution_only_attempts_api_fallback_once():
    client = TikTokLiveClient(unique_id="offline")
    client.web.get = AsyncMock(side_effect=[httpx.Response(200, text="changed HTML"), httpx.ReadTimeout("api")])
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.start()
        assert client.web.get.await_count == 2
    finally:
        await client.close()


async def test_cancel_disconnect_propagates_after_releasing_http():
    client = TikTokLiveClient(unique_id="offline")
    started = asyncio.Event()

    async def pending_close():
        started.set()
        await asyncio.Event().wait()

    client._ws.disconnect = pending_close
    closing = asyncio.create_task(client.disconnect(close_client=True))
    await started.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert client.web.httpx_client.is_closed
    assert client.web.signer.client.is_closed


async def test_cancel_while_waiting_for_disconnect_lock_closes_http():
    client = TikTokLiveClient(unique_id="offline")
    await client._disconnect_lock.acquire()
    closing = asyncio.create_task(client.disconnect(close_client=True))
    try:
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert client.web.httpx_client.is_closed
        assert client.web.signer.client.is_closed
    finally:
        client._disconnect_lock.release()
        await client.close()


def test_decompression_has_an_explicit_memory_limit():
    import gzip

    from TikTokLive.client.ws.ws_utils import extract_webcast_response_message
    from TikTokLive.proto.custom_extras import WebcastPushFrame

    frame = WebcastPushFrame(headers={"compress_type": "gzip"}, payload=gzip.compress(b"x" * 2048))
    with pytest.raises(ValueError, match="Decompressed"):
        extract_webcast_response_message(frame, max_payload_size=1024)
    with pytest.raises(ValueError, match="size limit"):
        extract_webcast_response_message(WebcastPushFrame(payload=b"x" * 2048), max_payload_size=1024)
