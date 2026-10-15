import asyncio
import time
from dataclasses import replace

import pytest
from conftest import Enricher, Socket, eventually

from live_service.manager import ConnectionManager
from live_service.upstream.client import AnonymousLiveClient
from live_service.upstream.errors import UpstreamError
from TikTokLive.client.errors import (
    UserNotFoundError,
    UserOfflineError,
    WebcastBlocked200Error,
)
from TikTokLive.events import ConnectEvent


class ScriptedSDK(AnonymousLiveClient):
    """Exercise the manager's SDK options without opening HTTP pools."""

    def __init__(self, start_script):
        self.event_sink = None
        self.start_script = start_script
        self.runtime = asyncio.get_running_loop().create_future()
        self.closed = False

    async def start(self, **kwargs):
        await self.start_script(self, kwargs)
        return self.runtime

    def snapshot(self):
        return {}

    async def disconnect(self, close_client=False):
        self.closed = True
        if not self.runtime.done():
            self.runtime.set_result(None)


class ScriptedFactory:
    def __init__(self, second_error=None):
        self.clients = []
        self.calls = []
        self.second_error = second_error

    def __call__(self, key):
        client = ScriptedSDK(self.start)
        self.clients.append(client)
        return client

    async def start(self, client, kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 2 and self.second_error is not None:
            raise self.second_error
        await client.event_sink(ConnectEvent(unique_id="room", room_id=7 if len(self.calls) == 1 else 8))


def handshake_error(status):
    return UpstreamError("handshake_rejected", terminal=True, refresh_room=status in {400, 404})


@pytest.mark.parametrize("wait_reason", ["semaphore", "upstream_cooldown"])
async def test_cached_room_expiry_is_checked_after_connect_admission(settings, wait_reason):
    factory = ScriptedFactory()
    manager = ConnectionManager(
        replace(settings, room_id_cache_ttl=0.02, fast_reconnect_delay=0.001, connect_concurrency=1),
        client_factory=factory,
        enricher=Enricher(),
    )
    held = False
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        if wait_reason == "semaphore":
            await manager._connect_limit.acquire()
            held = True
        else:
            manager._upstream_not_before = time.monotonic() + 0.08
        factory.clients[0].runtime.set_exception(ConnectionResetError())
        if held:
            await eventually(lambda: len(factory.clients) == 2)
            await asyncio.sleep(0.04)
            manager._connect_limit.release()
            held = False
        await eventually(lambda: len(factory.calls) == 2)
        assert "room_id" not in factory.calls[1]
        assert factory.calls[1].get("fetch_live_check", True)
    finally:
        if held:
            manager._connect_limit.release()
        await manager.close()


@pytest.mark.parametrize(
    "rejection",
    [
        UserOfflineError(),
        UserNotFoundError("room"),
        handshake_error(400),
        handshake_error(404),
        WebcastBlocked200Error("cached room rejected"),
    ],
)
async def test_room_specific_cached_rejection_resolves_creator_once(settings, rejection):
    factory = ScriptedFactory(rejection)
    manager = ConnectionManager(
        replace(settings, fast_reconnect_delay=0.001), client_factory=factory, enricher=Enricher(),
    )
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        factory.clients[0].runtime.set_exception(ConnectionResetError())
        await eventually(lambda: len(factory.calls) >= 2)
        await eventually(lambda: len(factory.calls) == 3 or not manager.tasks)
        assert len(factory.calls) == 3
        assert factory.calls[1]["room_id"] == 7
        assert "room_id" not in factory.calls[2]
        assert manager.rooms["room"].room_id == 8
        assert manager.health()["reconnects"] == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("rejection", [handshake_error(400), handshake_error(404), WebcastBlocked200Error("room rejected")])
async def test_room_specific_rejection_is_terminal_after_fresh_fallback(settings, rejection):
    factory = ScriptedFactory()
    start_script = factory.start

    async def fail_after_first_connection(client, kwargs):
        if factory.calls:
            factory.calls.append(kwargs)
            raise rejection
        await start_script(client, kwargs)

    factory.start = fail_after_first_connection
    manager = ConnectionManager(
        replace(settings, fast_reconnect_delay=0.001), client_factory=factory, enricher=Enricher(),
    )
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        factory.clients[0].runtime.set_exception(ConnectionResetError())
        await eventually(lambda: len(factory.calls) >= 2)
        await eventually(lambda: not manager.tasks)
        assert len(factory.calls) == 3
        assert factory.calls[1]["room_id"] == 7
        assert "room_id" not in factory.calls[2]
        assert manager.rooms["room"].state == "stopped"
    finally:
        await manager.close()


@pytest.mark.parametrize("status", [401, 403])
async def test_cached_handshake_auth_rejection_stays_terminal(settings, status):
    factory = ScriptedFactory(handshake_error(status))
    manager = ConnectionManager(
        replace(settings, fast_reconnect_delay=0.001), client_factory=factory, enricher=Enricher(),
    )
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        factory.clients[0].runtime.set_exception(ConnectionResetError())
        await eventually(lambda: len(factory.calls) == 2 and not manager.tasks)
        assert manager.rooms["room"].state == "stopped"
    finally:
        await manager.close()


async def test_cached_rate_limit_keeps_global_cooldown(settings):
    rejection = UpstreamError("rate_limited", retry_after=1)
    factory = ScriptedFactory(rejection)
    manager = ConnectionManager(
        replace(settings, fast_reconnect_delay=0.001), client_factory=factory, enricher=Enricher(),
    )
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        factory.clients[0].runtime.set_exception(ConnectionResetError())
        await eventually(lambda: manager._upstream_not_before > time.monotonic())
        assert manager._upstream_not_before - time.monotonic() > 0.9
        assert manager.rooms["room"].room_id == 7
        await asyncio.sleep(0.03)
        assert len(factory.calls) == 2
    finally:
        await manager.close()


async def test_flapping_connections_get_only_one_fast_retry_until_stable(settings, monkeypatch):
    factory = ScriptedFactory()
    delay_samples = []

    def uniform(low, high):
        delay_samples.append((low, high))
        return high

    monkeypatch.setattr("live_service.manager.random.uniform", uniform)
    manager = ConnectionManager(
        replace(settings, fast_reconnect_delay=0.001), client_factory=factory, enricher=Enricher(),
    )
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        for attempt in range(3):
            factory.clients[-1].runtime.set_exception(ConnectionResetError())
            await eventually(lambda: len(factory.calls) == attempt + 2 and manager.live_connected.get("room"))
        assert sum(low == 0 and high == 0.001 for low, high in delay_samples) == 1
        manager.rooms["room"].connected_at = time.monotonic() - settings.retry_reset_after - 1
        factory.clients[-1].runtime.set_exception(ConnectionResetError())
        await eventually(lambda: len(factory.calls) == 5 and manager.live_connected.get("room"))
        assert sum(low == 0 and high == 0.001 for low, high in delay_samples) == 2
    finally:
        await manager.close()
