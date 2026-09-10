import asyncio
from dataclasses import replace

import pytest
from conftest import Enricher, Factory, Socket, eventually

from live_service.manager import ConnectionManager
from live_service.upstream.bootstrap import AnonymousBootstrap
from live_service.upstream.client import AnonymousLiveClient
from TikTokLive.client.errors import UserOfflineError
from TikTokLive.events import ConnectEvent


@pytest.mark.parametrize("limit", ["max_rooms", "max_subscribers", "max_subscribers_per_room"])
async def test_capacity_rejection_does_not_allocate_collector(settings, limit):
    factory = Factory()
    manager = ConnectionManager(replace(settings, **{limit: 1}), client_factory=factory, enricher=Enricher())
    accepted, rejected = Socket(), Socket()
    try:
        await manager.connect(accepted, "room")
        await eventually(lambda: "LIVING" in accepted.messages)
        await manager.connect(rejected, "other" if limit == "max_rooms" else "room")
        assert rejected.closed and rejected.close_code == 1013
        assert len(factory.clients) == len(manager.rooms) == 1
        assert manager.health()["subscribers"] == 1
        assert not accepted.closed
    finally:
        await manager.close()


async def test_short_disconnect_uses_cached_room_once_then_resolves_fresh(settings, monkeypatch):
    calls, clients, runtimes = [], [], []

    async def start(client, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise UserOfflineError()
        await client.event_sink(ConnectEvent(unique_id="room", room_id=7 if len(calls) == 1 else 8))
        runtime = asyncio.get_running_loop().create_future()
        runtimes.append(runtime)
        return runtime

    def factory(key):
        client = AnonymousLiveClient(key, AnonymousBootstrap(), settings)
        clients.append(client)
        return client

    monkeypatch.setattr(AnonymousLiveClient, "start", start)
    manager = ConnectionManager(replace(settings, fast_reconnect_delay=0.001), client_factory=factory, enricher=Enricher())
    socket = Socket()
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)
        runtimes[0].set_exception(ConnectionResetError())
        await eventually(lambda: len(calls) == 3 and socket.messages.count("LIVING") == 2)
        assert calls[0] == {}
        assert "room_id" not in calls[0]
        assert calls[1]["room_id"] == 7
        assert "room_id" not in calls[2]
        assert manager.rooms["room"].room_id == 8
        assert manager.health()["reconnects"] == 1
        assert 0 < manager.rooms["room"].last_recovery_seconds < 1
    finally:
        await manager.close()
