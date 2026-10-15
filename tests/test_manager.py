import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from conftest import Enricher, Factory, Socket, comment, eventually

from live_service.delivery import Subscriber
from live_service.manager import ConnectionManager
from live_service.upstream.errors import UpstreamError
from TikTokLive import TikTokLiveClient
from TikTokLive.client.errors import UserOfflineError
from TikTokLive.events import ControlEvent
from TikTokLive.proto import ProtoMessageFetchResult, ProtoMessageFetchResultBaseProtoMessage
from TikTokLive.proto.custom_proto import ControlAction


async def test_simultaneous_subscribers_share_one_collector(settings):
    factory = Factory()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    sockets = [Socket() for _ in range(100)]
    try:
        await asyncio.gather(*(manager.connect(s, "@room" if i % 2 else "room") for i, s in enumerate(sockets)))
        await eventually(lambda: all("LIVING" in s.messages for s in sockets))
        assert len(factory.clients) == len(manager.tasks) == 1
        assert all(s.messages[:2] == ["CONNECTING", "LIVING"] for s in sockets)
        await factory.clients[0].event_sink(comment())
        await eventually(lambda: all(any(m.startswith("{") for m in s.messages) for s in sockets))
        assert all(s.max_writers == 1 for s in sockets)
    finally:
        await manager.close()
    assert all(s.closed for s in sockets)
    assert all(c.closed for c in factory.clients)


async def test_reconnect_refreshes_client_and_rejects_old_generation(settings):
    factory, socket = Factory(), Socket()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)
        old = factory.clients[0]
        old.runtime.set_exception(ConnectionResetError())
        await eventually(lambda: len(factory.clients) == 2 and socket.messages.count("LIVING") == 2)
        assert old.closed
        await old.event_sink(comment(100, "stale"))
        await factory.clients[-1].event_sink(comment(101, "new"))
        await manager.rooms["room"].queue.join()
        await eventually(lambda: any('"new"' in text for text in socket.messages))
        assert not any('"stale"' in text for text in socket.messages)
        assert socket.messages[:5] == ["CONNECTING", "LIVING", "UPSTREAM_RECONNECTING", "CONNECTING", "LIVING"]
    finally:
        await manager.close()


async def test_natural_close_without_control_recovers(settings):
    factory = Factory()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        factory.clients[0].runtime.set_result(None)
        await eventually(lambda: len(factory.clients) == 2 and manager.live_connected.get("room"))
    finally:
        await manager.close()


async def test_stable_connection_reconnects_without_previous_failure_backoff(settings):
    factory = Factory(errors=[ConnectionResetError(), ConnectionResetError()])
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    failures = []
    retry_delay = manager._retry_delay

    def observe_retry(error, attempt):
        failures.append(attempt)
        return retry_delay(error, attempt)

    manager._retry_delay = observe_retry
    try:
        await manager.connect(Socket(), "room")
        await eventually(lambda: manager.live_connected.get("room"))
        manager.rooms["room"].connected_at = time.monotonic() - settings.retry_reset_after - 1
        factory.clients[-1].runtime.set_exception(ConnectionResetError())
        await eventually(lambda: len(factory.clients) == 4 and manager.live_connected.get("room"))
        assert failures == [0, 1, 0]
    finally:
        await manager.close()


@pytest.mark.parametrize("action,status", [(1, "1"), (2, "2"), (3, "3"), (4, "3"), (0, "0")])
async def test_control_wire_values_unchanged(settings, action, status):
    factory, socket = Factory(), Socket()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)
        await factory.clients[0].event_sink(ControlEvent(action=ControlAction(action)))
        await eventually(lambda: status in socket.messages)
        if action in (3, 4):
            factory.clients[0].runtime.set_result(None)
            await eventually(lambda: factory.clients[0].closed)
            await asyncio.sleep(settings.retry_max * 2)
            assert len(factory.clients) == 1
            assert not manager.live_connected["room"]
            assert "LIVE_CONNECT_ERROR" not in socket.messages
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "actions,expected",
    [
        ([1, 2, 1, 2, 3], ["1", "2", "1", "2", "3"]),
        ([4], ["3"]),
        ([1, 1], ["1", "1"]),
    ],
)
async def test_sdk_control_frames_match_legacy_subscription(settings, monkeypatch, actions, expected):
    """One frame produces derived + original events, but only one business status."""
    factory, socket = Factory(), Socket()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    sdk = TikTokLiveClient(unique_id="room")
    legacy_statuses = []
    sdk.on(ControlEvent, lambda event: legacy_statuses.append({1: "1", 2: "2", 3: "3", 4: "3"}.get(event.action, "0")))

    async def responses(**kwargs):
        for action in actions:
            yield ProtoMessageFetchResult(messages=[ProtoMessageFetchResultBaseProtoMessage(
                method="WebcastControlMessage",
                payload=bytes(ControlEvent(action=ControlAction(action))),
            )])

    monkeypatch.setattr(sdk._ws, "connect", responses)
    # The fixture has no real SDK transport to close when a stream-end is parsed.
    monkeypatch.setattr(sdk, "disconnect", AsyncMock())
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)
        sdk.event_sink = factory.clients[0].event_sink
        await sdk._ws_client_loop(ProtoMessageFetchResult(), True, True)
        room = manager.rooms["room"]
        await room.queue.join()
        await room.subscribers[socket].queue.join()
        assert legacy_statuses == expected
        assert socket.messages[2:] == legacy_statuses
    finally:
        await manager.close()
        await sdk.close()


async def test_offline_is_terminal_and_state_not_living(settings):
    factory, socket = Factory(errors=[UserOfflineError()]), Socket()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVE_CONNECT_ERROR" in socket.messages)
        await asyncio.sleep(settings.retry_max * 2)
        assert len(factory.clients) == 1
        assert not manager.live_connected["room"]
        assert not manager.tasks
    finally:
        await manager.close()


async def test_start_timeout_retries_and_cleans_partial_client(settings):
    factory = Factory(errors=[UpstreamError("bootstrap_timeout")])
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    socket = Socket()
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)
        assert "UPSTREAM_TIMEOUT" in socket.messages
        assert len(factory.clients) == 2
        assert factory.clients[0].closed
    finally:
        await manager.close()


async def test_idle_grace_reuses_client_then_releases(settings):
    factory = Factory()
    manager = ConnectionManager(replace(settings, idle_grace=0.05), client_factory=factory, enricher=Enricher())
    a, b = Socket(), Socket()
    try:
        await manager.connect(a, "room")
        await eventually(lambda: "LIVING" in a.messages)
        await manager.remove(a, "room")
        await manager.connect(b, "room")
        await eventually(lambda: "LIVING" in b.messages)
        assert len(factory.clients) == 1
        assert b.messages[0] == "LIVING"
        await manager.remove(b, "room")
        await eventually(lambda: not manager.rooms and factory.clients[0].closed)
    finally:
        await manager.close()


async def test_slow_subscriber_isolated_with_one_writer(settings):
    factory = Factory()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    slow, fast = Socket(blocked=True), Socket()
    try:
        await manager.connect(slow, "room")
        await manager.connect(fast, "room")
        await eventually(lambda: "LIVING" in fast.messages)
        for number in range(20):
            await factory.clients[0].event_sink(comment(number))
        await manager.send(fast, "room", "pong")
        await eventually(lambda: len([m for m in fast.messages if m.startswith("{")]) == 20)
        assert fast.max_writers == 1
        assert "pong" in fast.messages
        await eventually(lambda: slow.closed)
        assert not fast.closed
        assert manager.live_connected["room"]
        ids = [json.loads(m)["dyMsgId"] for m in fast.messages if m.startswith("{")]
        assert ids == [str(i) for i in range(20)]
    finally:
        await manager.close()


async def test_ingress_backpressure_is_bounded_without_losing_comments(settings):
    gate, factory = asyncio.Event(), Factory()
    manager = ConnectionManager(replace(settings, room_queue_size=2), client_factory=factory, enricher=Enricher(gate))
    socket = Socket()
    producer = None
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)

        async def produce():
            for i in range(50):
                await factory.clients[0].event_sink(comment(i))

        producer = asyncio.create_task(produce())
        await eventually(lambda: manager.rooms["room"].queue.full())
        assert not producer.done()
        gate.set()
        await producer
        await eventually(lambda: len([m for m in socket.messages if m.startswith("{")]) == 50)
    finally:
        gate.set()
        if producer:
            await producer
        await manager.close()


async def test_shutdown_during_start_cleans_every_client(settings):
    factory = Factory(gate=asyncio.Event())
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    await asyncio.gather(*(manager.connect(Socket(), str(i)) for i in range(10)))
    await eventually(lambda: len(factory.clients) == 10)
    await asyncio.wait_for(manager.close(), 1)
    assert all(client.closed for client in factory.clients)
    assert not manager.rooms


async def test_oversized_subscriber_message_closes_even_before_sender_starts(settings):
    socket, removed = Socket(), asyncio.Event()

    async def remove():
        removed.set()

    subscriber = Subscriber(socket, replace(settings, subscriber_queue_bytes=1), remove)
    assert not subscriber.enqueue("large")
    await asyncio.wait_for(removed.wait(), 1)
    assert socket.closed and socket.close_code == 1013
    await subscriber.stop()


async def test_repeated_room_lifecycles_leave_no_background_tasks(settings):
    factory = Factory()
    manager = ConnectionManager(replace(settings, idle_grace=0), client_factory=factory, enricher=Enricher())
    try:
        for i in range(100):
            socket = Socket()
            await manager.connect(socket, "room")
            await eventually(lambda: "LIVING" in socket.messages)
            await manager.remove(socket, "room")
            await eventually(lambda: not manager.rooms)
        await eventually(lambda: all(client.closed for client in factory.clients))
    finally:
        await manager.close()


def test_account_rate_limit_applies_to_other_rooms(settings):
    manager = ConnectionManager(settings, enricher=Enricher())
    error = UpstreamError("rate_limited", retry_after=7)
    assert manager._retry_delay(error, 0) == 7
    assert manager._upstream_not_before > 0


async def test_source_dedup_is_per_subscriber_within_and_across_upstream_connections(settings):
    factory, existing, newcomer = Factory(), Socket(), Socket()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    try:
        await manager.connect(existing, "room")
        await eventually(lambda: "LIVING" in existing.messages)
        await factory.clients[0].event_sink(comment(7, "same content"))
        await factory.clients[0].event_sink(comment(7, "same content"))
        await eventually(lambda: any('"dyMsgId": "7"' in m for m in existing.messages))
        factory.clients[0].runtime.set_exception(ConnectionResetError())
        await eventually(lambda: len(factory.clients) == 2 and manager.live_connected["room"])
        await manager.connect(newcomer, "room")
        await factory.clients[-1].event_sink(comment(7, "same content"))
        await factory.clients[-1].event_sink(comment(8, "same content"))
        await factory.clients[-1].event_sink(comment(8, "same content"))
        await eventually(lambda: any('"dyMsgId": "8"' in m for m in existing.messages))
        await eventually(lambda: any('"dyMsgId": "8"' in m for m in newcomer.messages))
        assert sum('"dyMsgId": "7"' in m for m in existing.messages) == 1
        assert sum('"dyMsgId": "7"' in m for m in newcomer.messages) == 1
        await manager.rooms["room"].queue.join()
        for peer in manager.rooms["room"].subscribers.values():
            await peer.queue.join()
        assert sum('"dyMsgId": "8"' in m for m in existing.messages) == 1
        assert sum('"dyMsgId": "8"' in m for m in newcomer.messages) == 1
        assert manager.rooms["room"].subscribers[existing].suppressed_duplicates == 2
        assert manager.rooms["room"].subscribers[existing].suppressed_replays == 1
        assert len(manager.rooms["room"].subscribers[existing]._seen) <= settings.replay_dedup_size
    finally:
        await manager.close()
