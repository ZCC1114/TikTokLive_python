from __future__ import annotations

import asyncio
import socket
import time
from collections import Counter
from contextlib import suppress

from websockets.exceptions import InvalidStatusCode
from websockets.legacy.client import connect

from live_service.config import Settings
from live_service.upstream.bootstrap import AnonymousBootstrap, normalize_username
from live_service.upstream.errors import UpstreamError, retry_after_seconds
from live_service.upstream.protocol import (
    MAX_PAYLOAD,
    ack,
    business_event,
    connection_url,
    decode_frame,
    enter_room,
    heartbeat,
)
from TikTokLive.client.diagnostics import CommentDiagnostics
from TikTokLive.events import CommentEvent, ConnectEvent, ControlEvent
from TikTokLive.proto.custom_proto import ControlAction


class AnonymousLiveClient:
    """One transport generation; the room supervisor owns reconnection policy."""

    def __init__(self, unique_id: str, bootstrap: AnonymousBootstrap, settings: Settings, *, ws_url=None):
        self.unique_id = normalize_username(unique_id)
        self.bootstrap = bootstrap
        self.settings = settings
        self.event_sink = None
        self.websocket = None
        self._ws_url = ws_url  # In-process loopback integration tests only; no environment override.
        self._runtime = None
        self._starting = False
        self._closed = False
        self._sequence = 0
        self.connection_phase = "idle"
        self.connect_timings = {}
        self.comment_diagnostics = CommentDiagnostics()
        self.counters = Counter()
        self.last_frame_at = 0.0
        self.room_id = 0

    @property
    def connected(self):
        return bool(self.websocket and self.websocket.open)

    async def _emit(self, event):
        if self.event_sink is not None:
            await self.event_sink(event)

    async def start(self, *, room_id: int | None = None):
        if self._starting or self._runtime is not None or self._closed:
            raise RuntimeError("Client generation cannot be started twice")
        self._starting = True
        started = time.monotonic()
        try:
            self.connection_phase = "anonymous_bootstrap"
            credentials = await self.bootstrap.resolve(self.unique_id, room_id=room_id)
            self.connect_timings["bootstrap"] = credentials.diagnostics
            self.room_id = int(credentials.room_id)
            self.connection_phase = "handshake"
            handshake_started = time.monotonic()
            try:
                self.websocket = await connect(
                    self._ws_url or connection_url(self.room_id, self.settings.upstream_heartbeat_interval),
                    extra_headers={"Cookie": credentials.cookie, "Origin": "https://www.tiktok.com",
                                   "Referer": "https://www.tiktok.com/", "Accept-Language": "en-US,en;q=0.9",
                                   "Cache-Control": "no-cache"},
                    user_agent_header=credentials.user_agent, ping_interval=None, ping_timeout=None,
                    open_timeout=self.settings.upstream_open_timeout, close_timeout=0.5,
                    max_size=MAX_PAYLOAD, max_queue=16,
                )
            except InvalidStatusCode as exc:
                self.counters["handshake_rejections"] += 1
                if exc.status_code == 429:
                    raise UpstreamError("rate_limited", retry_after=retry_after_seconds(
                        exc.headers.get("Retry-After"))) from None
                # Refresh only the cached room lookup. Never refresh identity on rejection.
                raise UpstreamError("handshake_rejected", terminal=True,
                                    refresh_room=exc.status_code in {200, 400, 404}) from None
            finally:
                self.connect_timings["handshake_seconds"] = round(time.monotonic() - handshake_started, 3)
            self._configure_socket()
            await self._send(enter_room(self.room_id))
            await self._send_heartbeat()
            self.last_frame_at = time.monotonic()
            self.connection_phase = "connected"
            await self._emit(ConnectEvent(unique_id=self.unique_id, room_id=self.room_id))
            self._runtime = asyncio.create_task(self._run(), name=f"live-upstream:{self.unique_id}")
            return self._runtime
        except BaseException:
            await self._close_socket()
            raise
        finally:
            self.connect_timings["total_seconds"] = round(time.monotonic() - started, 3)
            self._starting = False

    def _configure_socket(self):
        sock = self.websocket.transport.get_extra_info("socket")
        if sock is None:
            return
        with suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 3), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3), ("TCP_USER_TIMEOUT", 8000)):
            option = getattr(socket, name, None)
            if option is not None:
                with suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP, option, value)

    async def _send(self, payload):
        await asyncio.wait_for(self.websocket.send(payload), self.settings.upstream_send_timeout)

    async def _send_heartbeat(self):
        self._sequence += 1
        await self._send(heartbeat(self.room_id, self._sequence))
        self.counters["heartbeats_sent"] += 1

    async def _heartbeats(self):
        while True:
            await asyncio.sleep(self.settings.upstream_heartbeat_interval)
            await self._send_heartbeat()

    async def _receive(self):
        while True:
            try:
                raw = await asyncio.wait_for(self.websocket.recv(), self.settings.upstream_idle_timeout)
            except asyncio.TimeoutError:
                self.counters["idle_timeouts"] += 1
                raise UpstreamError("upstream_idle_timeout") from None
            self.last_frame_at = time.monotonic()
            self.counters["frames"] += 1
            frame, response = decode_frame(raw)
            if response is None:
                self.counters["non_message_frames"] += 1
                continue
            if response.needs_ack:
                await self._send(ack(frame.log_id, response.internal_ext))
                self.counters["acks_sent"] += 1
            for message in response.messages:
                self.counters["messages"] += 1
                event = business_event(message, self.room_id)
                if event is None:
                    continue
                if isinstance(event, CommentEvent):
                    self.counters["comments"] += 1
                    self.counters["history_comments"] += int(message.is_history)
                    self.comment_diagnostics.observe(self.room_id, event.base_message.message_id,
                                                     initial=False, payload=message.payload)
                    # Aggregate arrival age only; timestamps, identities and content aren't persisted.
                    created = event.base_message.create_time
                    if created > 10**12:
                        created /= 1000
                    age = time.time() - created
                    if not message.is_history and 0 <= age <= 60:
                        self.counters["timed_comments"] += 1
                        self.counters["arrival_age_le_2s"] += int(age <= 2)
                        self.counters["arrival_age_le_5s"] += int(age <= 5)
                await self._emit(event)
                if type(event) is ControlEvent and event.action in {
                    ControlAction.CONTROL_ACTION_STREAM_ENDED, ControlAction.CONTROL_ACTION_STREAM_SUSPENDED,
                }:
                    return
            await asyncio.sleep(0)

    async def _run(self):
        reader = asyncio.create_task(self._receive(), name=f"live-receiver:{self.unique_id}")
        heartbeats = asyncio.create_task(self._heartbeats(), name=f"live-heartbeat:{self.unique_id}")
        try:
            completed, _ = await asyncio.wait((reader, heartbeats), return_when=asyncio.FIRST_COMPLETED)
            # Surface heartbeat failures even when the reader is blocked by queue pressure.
            if heartbeats in completed:
                await heartbeats
                raise UpstreamError("heartbeat_stopped")
            await reader
        finally:
            for task in (reader, heartbeats):
                task.cancel()
            await asyncio.gather(reader, heartbeats, return_exceptions=True)
            await self._close_socket()

    async def _close_socket(self):
        if self.websocket is not None:
            try:
                await asyncio.wait_for(self.websocket.close(), 0.8)
            except (Exception, asyncio.CancelledError):
                self.websocket.transport.abort()
            finally:
                if not self.websocket.closed:
                    self.websocket.transport.abort()

    async def disconnect(self, close_client=False):
        self._closed = True
        if self._runtime is not None and self._runtime is not asyncio.current_task():
            self._runtime.cancel()
            await asyncio.gather(self._runtime, return_exceptions=True)
        await self._close_socket()

    def snapshot(self):
        return {"transport": "anonymous_unsigned", "counters": dict(self.counters),
                "last_frame_age_seconds": round(time.monotonic() - self.last_frame_at, 3) if self.last_frame_at else None}
