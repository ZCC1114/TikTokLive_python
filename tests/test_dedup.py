import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import Enricher, Factory, Socket, comment, eventually

from live_service.delivery import Subscriber
from live_service.manager import ConnectionManager
from TikTokLive import TikTokLiveClient
from TikTokLive.client.diagnostics import CommentDiagnostics
from TikTokLive.events import CommentEvent
from TikTokLive.proto import ProtoMessageFetchResult, ProtoMessageFetchResultBaseProtoMessage


async def test_repeated_source_chat_frames_are_delivered_once(settings, monkeypatch):
    """A repeated upstream chat frame has one SDK event but only one business delivery."""
    factory, socket = Factory(), Socket()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    sdk = TikTokLiveClient(unique_id="room")
    sdk._room_id = 7300000000000000001
    source_events = []
    sdk.on(CommentEvent, lambda event: source_events.append(event.base_message.message_id))

    async def responses(**kwargs):
        # The signer initial response and live WS can repeat a source message.
        # Different IDs with identical text are distinct viewer messages.
        for first, ids in [(True, [7, 8]), (False, [7, 9, 9])]:
            yield ProtoMessageFetchResult(is_first=first, messages=[
                ProtoMessageFetchResultBaseProtoMessage(
                    method="WebcastChatMessage", payload=bytes(comment(message_id, "same content")),
                )
                for message_id in ids
            ])

    monkeypatch.setattr(sdk._ws, "connect", responses)
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)
        sdk.event_sink = factory.clients[0].event_sink
        await sdk._ws_client_loop(ProtoMessageFetchResult(), True, True)
        room = manager.rooms["room"]
        await room.queue.join()
        await room.subscribers[socket].queue.join()
        assert source_events == [7, 8, 7, 9, 9]
        messages = [json.loads(text) for text in socket.messages if text.startswith("{")]
        assert [message["dyMsgId"] for message in messages] == ["7", "8", "9"]
        assert [message["danmuContent"] for message in messages] == ["same content"] * 3
        assert room.subscribers[socket].suppressed_duplicates == 2
        assert room.subscribers[socket].suppressed_replays == 0
        source = sdk.comment_diagnostics.snapshot()
        assert source["signer_comments"] == 2
        assert source["websocket_comments"] == 3
        assert source["signer_to_websocket_replays"] == 1
        assert source["websocket_duplicates"] == 1
        assert source["repeated_ids_payload_changed"] == 0
    finally:
        await manager.close()
        await sdk.close()


async def test_unknown_source_ids_remain_deliverable(settings):
    factory, socket = Factory(), Socket()
    manager = ConnectionManager(settings, client_factory=factory, enricher=Enricher())
    try:
        await manager.connect(socket, "room")
        await eventually(lambda: "LIVING" in socket.messages)
        for _ in range(2):
            await factory.clients[0].event_sink(comment(0, "same content"))
        await manager.rooms["room"].queue.join()
        await manager.rooms["room"].subscribers[socket].queue.join()
        messages = [json.loads(text) for text in socket.messages if text.startswith("{")]
        assert [message["dyMsgId"] for message in messages] == ["0", "0"]
    finally:
        await manager.close()


async def ignore_disconnect():
    pass


async def test_duplicate_cache_is_bounded_and_expires_from_first_delivery(settings, monkeypatch):
    socket = Socket()
    now = 100.0
    monkeypatch.setattr("live_service.delivery.time", SimpleNamespace(monotonic=lambda: now))
    subscriber = Subscriber(socket, replace(settings, replay_dedup_size=2, replay_dedup_ttl=10), ignore_disconnect)
    try:
        subscriber.enqueue("one", source_key=(1, 1), generation=1)
        subscriber.enqueue("one duplicate", source_key=(1, 1), generation=1)
        subscriber.enqueue("two", source_key=(1, 2), generation=1)
        await subscriber.queue.join()
        assert socket.messages == ["one", "two"]
        now += 9
        subscriber.enqueue("one reconnect replay", source_key=(1, 1), generation=2)
        assert len(subscriber._seen) == 2
        now += 2
        subscriber.enqueue("one after expiry", source_key=(1, 1), generation=2)
        subscriber.enqueue("three", source_key=(1, 3), generation=2)
        subscriber.enqueue("four", source_key=(1, 4), generation=2)
        assert len(subscriber._seen) == 2
        subscriber.enqueue("one after eviction", source_key=(1, 1), generation=2)
        await subscriber.queue.join()
        assert socket.messages == ["one", "two", "one after expiry", "three", "four", "one after eviction"]
        assert subscriber.suppressed_duplicates == 1
        assert subscriber.suppressed_replays == 1
    finally:
        await subscriber.stop()


@pytest.mark.parametrize("generation", [1, 2])
async def test_room_identity_and_disabled_dedup(settings, generation):
    socket = Socket()
    subscriber = Subscriber(socket, settings, ignore_disconnect)
    disabled_socket = Socket()
    disabled = Subscriber(disabled_socket, replace(settings, replay_dedup_size=0), ignore_disconnect)
    try:
        for peer in (subscriber, disabled):
            peer.enqueue("room one", source_key=(1, 1), generation=1)
            peer.enqueue("room two", source_key=(2, 1), generation=generation)
            peer.enqueue("duplicate", source_key=(1, 1), generation=generation)
            # Statuses and pongs have no source message ID and remain repeatable.
            peer.enqueue("pong")
            peer.enqueue("pong")
            await peer.queue.join()
        assert socket.messages == ["room one", "room two", "pong", "pong"]
        assert disabled_socket.messages == ["room one", "room two", "duplicate", "pong", "pong"]
        assert not disabled._seen
    finally:
        await subscriber.stop()
        await disabled.stop()


def test_source_diagnostics_separate_initial_history_from_websocket_repeats():
    diagnostics = CommentDiagnostics()
    for room, message, initial, payload in [
        (1, 7, True, b"first chat"),
        (1, 7, True, b"first chat"),
        (1, 7, False, b"first chat"),
        (1, 8, False, b"another chat"),
        (1, 8, False, b"another chat"),
        (1, 8, False, b"updated metadata"),
        (2, 7, False, b"different room"),
        (1, 0, False, b"unknown ID"),
    ]:
        diagnostics.observe(room, message, initial=initial, payload=payload)
    assert diagnostics.snapshot() == {
        "signer_comments": 2,
        "websocket_comments": 6,
        "invalid_source_ids": 1,
        "signer_duplicates": 1,
        "signer_to_websocket_replays": 1,
        "websocket_duplicates": 2,
        "other_source_repeats": 0,
        "repeated_ids_payload_changed": 1,
        "tracked_source_ids": 3,
    }
    assert all(isinstance(value, int) for value in diagnostics.snapshot().values())
    assert all(len(record[1]) == 32 for record in diagnostics._seen.values())


def test_source_diagnostics_expiry_capacity_and_disabled_observer(monkeypatch):
    now = 100.0
    monkeypatch.setattr("TikTokLive.client.diagnostics.time", SimpleNamespace(monotonic=lambda: now))
    diagnostics = CommentDiagnostics(max_entries=2, ttl=10)
    for message_id in [1, 2, 3]:
        diagnostics.observe(1, message_id, initial=False, payload=b"payload")
    assert diagnostics.snapshot()["tracked_source_ids"] == 2
    diagnostics.observe(1, 1, initial=False, payload=b"payload")
    assert diagnostics.snapshot()["websocket_duplicates"] == 0
    now += 11
    diagnostics.observe(1, 1, initial=False, payload=b"payload")
    assert diagnostics.snapshot()["websocket_duplicates"] == 0
    assert diagnostics.snapshot()["tracked_source_ids"] == 1
    disabled = CommentDiagnostics(max_entries=0)
    for _ in range(2):
        disabled.observe(1, 1, initial=False, payload=b"payload")
    assert disabled.snapshot()["websocket_comments"] == 2
    assert disabled.snapshot()["tracked_source_ids"] == 0


@pytest.mark.parametrize("size,ttl", [(-1, 1), (1, 0), (1, float("nan"))])
def test_source_diagnostics_reject_invalid_limits(size, ttl):
    with pytest.raises(ValueError):
        CommentDiagnostics(max_entries=size, ttl=ttl)
