from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field

from live_service.config import Settings
from live_service.delivery import Subscriber
from live_service.enrichment import RedisEnricher
from live_service.protocol import comment_message, control_status
from live_service.upstream.bootstrap import AnonymousBootstrap, normalize_username
from live_service.upstream.client import AnonymousLiveClient
from live_service.upstream.errors import UpstreamError
from TikTokLive.client.errors import (
    AgeRestrictedError,
    UserNotFoundError,
    UserOfflineError,
    WebcastBlocked200Error,
)
from TikTokLive.events import CommentEvent, ConnectEvent, ControlEvent
from TikTokLive.proto.custom_proto import ControlAction

logger = logging.getLogger(__name__)


@dataclass
class QueuedComment:
    event: CommentEvent
    generation: int


@dataclass(eq=False)
class RoomSession:
    key: str
    queue: asyncio.Queue
    subscribers: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    worker: asyncio.Task | None = None
    idle_task: asyncio.Task | None = None
    client: object | None = None
    generation: int = 0
    connected: bool = False
    ended: bool = False
    stopping: bool = False
    connected_at: float = 0.0
    state: str = "connecting"
    last_error: str | None = None
    comments: int = 0
    metadata_available: bool = True
    room_id: int | None = None
    disconnected_at: float = 0.0
    reconnects: int = 0
    last_connect_seconds: float = 0.0
    last_recovery_seconds: float = 0.0
    last_connect_timings: dict = field(default_factory=dict)


class ConnectionManager:
    """Own room lifetimes independently of individual WebSocket writers."""

    def __init__(self, settings: Settings | None = None, *, client_factory=None, enricher=None):
        self.settings = settings or Settings.from_env()
        self.rooms: dict[str, RoomSession] = {}
        self.lock = asyncio.Lock()
        self.enricher = enricher or RedisEnricher(
            self.settings.redis_timeout,
            self.settings.redis_failure_cooldown,
        )
        self.client_factory = client_factory or self._make_client
        self.bootstrap = AnonymousBootstrap(timeout=self.settings.bootstrap_timeout,
                                            cookie_ttl=self.settings.anonymous_cookie_ttl)
        self._connect_limit = asyncio.Semaphore(self.settings.connect_concurrency)
        self._upstream_not_before = 0.0
        self._closing = False
        self._maintenance: set[asyncio.Task] = set()
        self._debug_raw = os.getenv("DEBUG_TIKTOK_RAW_COMMENT_EVENT", "0") == "1"

    def _make_client(self, live_id: str):
        return AnonymousLiveClient(live_id, self.bootstrap, self.settings)

    @staticmethod
    def _key(live_id: str) -> str:
        return normalize_username(live_id)

    @property
    def active_connections(self):
        return {key: set(room.subscribers) for key, room in self.rooms.items()}

    @property
    def clients(self):
        return {key: room.client for key, room in self.rooms.items() if room.client is not None}

    @property
    def tasks(self):
        return {key: room.task for key, room in self.rooms.items() if room.task and not room.task.done()}

    @property
    def live_connected(self):
        return {key: room.connected for key, room in self.rooms.items()}

    async def resolve_room_profile(self, username: str) -> dict:
        async def resolve():
            async with self._connect_limit:
                return (await self.bootstrap.resolve(username, require_live=False)).profile
        try:
            return await asyncio.wait_for(resolve(), self.settings.connect_timeout)
        except asyncio.TimeoutError:
            raise UpstreamError("bootstrap_timeout") from None

    async def connect(self, websocket, live_id: str) -> None:
        await websocket.accept()
        key = self._key(live_id)
        reject_code = 1012
        async with self.lock:
            existing = self.rooms.get(key)
            full = (
                (existing is None and len(self.rooms) >= self.settings.max_rooms)
                or sum(len(r.subscribers) for r in self.rooms.values()) >= self.settings.max_subscribers
                or (existing is not None and len(existing.subscribers) >= self.settings.max_subscribers_per_room)
            )
            if self._closing or full:
                room = None
                reject_code = 1013 if full else 1012
            else:
                room = self.rooms.get(key)
                if room is None:
                    room = RoomSession(key, asyncio.Queue(maxsize=self.settings.room_queue_size))
                    self.rooms[key] = room
                    room.worker = asyncio.create_task(self._consume(room), name=f"live-consumer:{key}")
                if room.idle_task is not None:
                    room.idle_task.cancel()
                    room.idle_task = None
                subscriber = Subscriber(websocket, self.settings, lambda: self.remove(websocket, key))
                room.subscribers[websocket] = subscriber
                # Register the initial status before any collector can publish.
                subscriber.enqueue("LIVING" if room.connected else "CONNECTING")
                if room.task is None or room.task.done():
                    room.task = asyncio.create_task(self._run_client(room), name=f"live-room:{key}")
        if room is None:
            await websocket.close(code=reject_code)

    async def remove(self, websocket, live_id: str) -> None:
        key = self._key(live_id)
        async with self.lock:
            room = self.rooms.get(key)
            subscriber = room.subscribers.pop(websocket, None) if room else None
            if room and subscriber and not room.subscribers and not room.stopping:
                if room.idle_task is None:
                    task = asyncio.create_task(self._stop_when_idle(room), name=f"live-idle:{key}")
                    room.idle_task = task
                    self._maintenance.add(task)
                    task.add_done_callback(self._maintenance.discard)
        if subscriber is not None:
            await subscriber.stop()

    async def send(self, websocket, live_id: str, text: str) -> None:
        room = self.rooms.get(self._key(live_id))
        subscriber = room.subscribers.get(websocket) if room else None
        if subscriber:
            subscriber.enqueue(text)

    async def broadcast(self, live_id: str, text: str) -> None:
        room = self.rooms.get(self._key(live_id))
        if room:
            self._broadcast(room, text)

    @staticmethod
    def _broadcast(room: RoomSession, text: str, *, source_key=None, generation: int = 0) -> None:
        for subscriber in tuple(room.subscribers.values()):
            subscriber.enqueue(text, source_key=source_key, generation=generation)

    async def _on_event(self, room: RoomSession, generation: int, event) -> None:
        if room.stopping or generation != room.generation or self.rooms.get(room.key) is not room:
            return
        if isinstance(event, ConnectEvent):
            room.connected = True
            room.connected_at = time.monotonic()
            room.state = "streaming"
            room.last_error = None
            room.room_id = int(event.room_id)
            if room.disconnected_at:
                room.last_recovery_seconds = time.monotonic() - room.disconnected_at
                room.reconnects += 1
                room.disconnected_at = 0.0
            await room.queue.put("LIVING")
        elif type(event) is ControlEvent:
            # The SDK also emits derived lifecycle events for this same frame.
            # Match the legacy ControlEvent subscription, not its subclasses.
            if event.action in {
                ControlAction.CONTROL_ACTION_STREAM_ENDED,
                ControlAction.CONTROL_ACTION_STREAM_SUSPENDED,
            }:
                room.ended = True
                room.connected = False
                room.state = "ended"
            elif event.action == ControlAction.CONTROL_ACTION_STREAM_PAUSED:
                room.state = "paused"
            elif event.action == ControlAction.CONTROL_ACTION_STREAM_UNPAUSED:
                room.state = "streaming"
            await room.queue.put(control_status(event.action))
        elif isinstance(event, CommentEvent):
            # The SDK awaits this sink: queue pressure cannot create unbounded
            # pyee callback tasks, and received comments retain their order.
            await room.queue.put(QueuedComment(event, generation))

    async def _consume(self, room: RoomSession) -> None:
        count = 0
        while True:
            item = await room.queue.get()
            try:
                if isinstance(item, str):
                    text = item
                    source_key = None
                    generation = 0
                else:
                    message = comment_message(item.event, debug_raw=self._debug_raw)
                    await self.enricher.enrich(message)
                    metadata_available = message.get("metadataAvailable") is True
                    if room.metadata_available != metadata_available:
                        room.metadata_available = metadata_available
                        self._broadcast(room, "METADATA_READY" if metadata_available else "METADATA_UNAVAILABLE")
                    text = json.dumps(message, ensure_ascii=False)
                    message_id = message["dyMsgId"]
                    source_key = (
                        (message["dyRoomId"], message_id) if message_id.isdigit() and int(message_id) > 0 else None
                    )
                    generation = item.generation
                    room.comments += 1
                self._broadcast(room, text, source_key=source_key, generation=generation)
            except Exception:
                logger.exception("Failed to process an event for room=%s", room.key)
            finally:
                room.queue.task_done()
            count += 1
            if count % 64 == 0:
                await asyncio.sleep(0)

    def _retry_delay(self, exc: Exception, attempt: int) -> float | None:
        if isinstance(exc, UpstreamError):
            if exc.retry_after is not None:
                delay = max(1.0, exc.retry_after)
                self._upstream_not_before = max(self._upstream_not_before, time.monotonic() + delay)
                return delay
            if exc.terminal:
                return None
        if isinstance(exc, (UserOfflineError, UserNotFoundError, AgeRestrictedError, WebcastBlocked200Error)):
            return None
        ceiling = min(self.settings.retry_max, self.settings.retry_initial * 2 ** min(attempt, 20))
        return random.uniform(ceiling / 2, ceiling)

    async def _run_client(self, room: RoomSession) -> None:
        attempt = 0
        fast_next = False
        fast_retry_available = True
        while not room.stopping and not self._closing:
            client = None
            delay = None
            room.generation += 1
            generation = room.generation
            room.ended = False
            room.state = "connecting"
            cache_candidate = fast_next
            use_cached_room = False
            fast_next = False
            try:
                client = self.client_factory(room.key)
                room.client = client
                client.event_sink = lambda event, token=generation: self._on_event(room, token, event)
                async with self._connect_limit:
                    # Honor TikTok-wide 429 cooldown even for already queued rooms.
                    while self._upstream_not_before > time.monotonic():
                        await asyncio.sleep(self._upstream_not_before - time.monotonic())
                    # Admission or a shared upstream cooldown may outlive the
                    # cached room. Check its age immediately before starting.
                    use_cached_room = bool(
                        cache_candidate and room.room_id and room.disconnected_at
                        and time.monotonic() - room.disconnected_at < self.settings.room_id_cache_ttl
                    )
                    connect_started = time.monotonic()
                    start_options = {}
                    if isinstance(client, AnonymousLiveClient):
                        if use_cached_room:
                            start_options.update(room_id=room.room_id)
                    try:
                        websocket_task = await asyncio.wait_for(
                            client.start(**start_options),
                            self.settings.fast_reconnect_timeout if use_cached_room else self.settings.connect_timeout,
                        )
                    finally:
                        room.last_connect_seconds = time.monotonic() - connect_started
                        room.last_connect_timings = dict(getattr(client, "connect_timings", {}))
                        logger.info(
                            "room=%s generation=%s cached_room=%s connect_seconds=%.3f stages=%s",
                            room.key, generation, use_cached_room, room.last_connect_seconds, room.last_connect_timings,
                        )
                await websocket_task
                if not room.ended:
                    raise ConnectionError("Upstream connection ended without a stream-end event")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                was_connected = room.connected
                room.connected = False
                room.last_error = exc.reason if isinstance(exc, UpstreamError) else type(exc).__name__
                if room.connected_at and time.monotonic() - room.connected_at >= self.settings.retry_reset_after:
                    attempt = 0
                    fast_retry_available = True
                delay = self._retry_delay(exc, attempt)
                room_rejected = isinstance(exc, (UserOfflineError, UserNotFoundError, WebcastBlocked200Error)) or (
                    isinstance(exc, UpstreamError) and exc.refresh_room
                )
                if use_cached_room and room_rejected:
                    # A cached room can have ended while the creator starts a new
                    # live. Resolve once before treating a room-specific rejection
                    # as terminal; authentication and rate limits keep their policy.
                    delay = self.settings.fast_reconnect_delay
                    room.room_id = None
                if was_connected and delay is not None:
                    room.disconnected_at = time.monotonic()
                    fast_next = True
                    if fast_retry_available and not (isinstance(exc, UpstreamError) and exc.retry_after is not None):
                        delay = random.uniform(0, self.settings.fast_reconnect_delay)
                        # A handshake followed immediately by another disconnect
                        # must not bypass backoff indefinitely. Stable service
                        # replenishes this one fast retry above.
                        fast_retry_available = False
                room.state = "retry_wait" if delay is not None else "stopped"
                logger.warning(
                    "room=%s generation=%s error=%s retry=%s", room.key, generation, type(exc).__name__, delay
                )
                await room.queue.put(
                    "LIVE_CONNECT_ERROR" if delay is None else (
                        "UPSTREAM_TIMEOUT" if isinstance(exc, asyncio.TimeoutError) or (
                            isinstance(exc, UpstreamError) and "timeout" in exc.reason
                        ) else "UPSTREAM_RECONNECTING"
                    )
                )
            finally:
                room.connected = False
                try:
                    if client is not None:
                        await self._close_client(client, room.key)
                finally:
                    room.client = None
            if room.ended or delay is None or room.stopping:
                return
            attempt += 1
            room.connected_at = 0.0
            await asyncio.sleep(delay)
            if not room.stopping:
                await room.queue.put("CONNECTING")

    async def _close_client(self, client, key: str) -> None:
        try:
            await asyncio.wait_for(client.disconnect(close_client=True), self.settings.cleanup_timeout)
        except asyncio.TimeoutError:
            logger.warning("Timed out closing upstream client for room=%s", key)
        except Exception:
            logger.exception("Upstream cleanup failed for room=%s", key)

    async def _stop_when_idle(self, room: RoomSession) -> None:
        try:
            await asyncio.sleep(self.settings.idle_grace)
            async with self.lock:
                if room.subscribers or self.rooms.get(room.key) is not room:
                    return
                room.stopping = True
                self.rooms.pop(room.key)
            await self._stop_room(room)
        finally:
            if room.idle_task is asyncio.current_task():
                room.idle_task = None

    async def _stop_room(self, room: RoomSession) -> None:
        room.stopping = True
        if room.idle_task and room.idle_task is not asyncio.current_task():
            room.idle_task.cancel()
            await asyncio.gather(room.idle_task, return_exceptions=True)
        if room.task:
            room.task.cancel()
            await asyncio.gather(room.task, return_exceptions=True)
        if room.worker:
            try:
                await asyncio.wait_for(room.queue.join(), self.settings.drain_timeout)
            except asyncio.TimeoutError:
                logger.warning("Room queue did not drain: room=%s count=%s", room.key, room.queue.qsize())
            room.worker.cancel()
            await asyncio.gather(room.worker, return_exceptions=True)
        await asyncio.gather(*(s.stop(drain=True) for s in tuple(room.subscribers.values())), return_exceptions=True)
        room.subscribers.clear()
        room.connected = False
        room.state = "closed"

    async def close(self) -> None:
        async with self.lock:
            self._closing = True
            rooms = list(self.rooms.values())
            self.rooms.clear()
            for room in rooms:
                room.stopping = True
        await asyncio.gather(*(self._stop_room(room) for room in rooms))
        if self._maintenance:
            await asyncio.gather(*tuple(self._maintenance), return_exceptions=True)
        await self.bootstrap.close()
        await self.enricher.aclose()

    def health(self) -> dict:
        return {
            "collector": "anonymous_unsigned",
            "anonymous_session": self.bootstrap.snapshot(),
            "ready": not self._closing,
            "rooms": len(self.rooms),
            "connected_rooms": sum(room.connected for room in self.rooms.values()),
            "subscribers": sum(len(room.subscribers) for room in self.rooms.values()),
            "pending_events": sum(room.queue.qsize() for room in self.rooms.values()),
            "pending_sends": sum(s.queue.qsize() for room in self.rooms.values() for s in room.subscribers.values()),
            "pending_send_bytes": sum(
                s.pending_bytes for room in self.rooms.values() for s in room.subscribers.values()
            ),
            "suppressed_replays": sum(
                s.suppressed_replays for room in self.rooms.values() for s in room.subscribers.values()
            ),
            "suppressed_duplicates": sum(
                s.suppressed_duplicates for room in self.rooms.values() for s in room.subscribers.values()
            ),
            "redis_failures": getattr(self.enricher, "failures", 0),
            "redis_degraded_messages": getattr(self.enricher, "degraded", 0),
            "reconnects": sum(room.reconnects for room in self.rooms.values()),
            "connection_details": {
                room.key: {
                    "state": room.state, "generation": room.generation,
                    "connection_phase": getattr(room.client, "connection_phase", None),
                    "last_error": room.last_error,
                    "upstream": room.client.snapshot() if isinstance(room.client, AnonymousLiveClient) else {},
                    "last_connect_seconds": round(room.last_connect_seconds, 3),
                    "last_recovery_seconds": round(room.last_recovery_seconds, 3),
                    "connect_timings": room.last_connect_timings,
                    "source_messages": (
                        room.client.comment_diagnostics.snapshot()
                        if getattr(room.client, "comment_diagnostics", None) is not None else {}
                    ),
                } for room in self.rooms.values()
            },
        }
