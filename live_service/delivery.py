from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from live_service.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class PendingMessage:
    text: str
    size: int
    queued_at: float


class Subscriber:
    """One writer per socket; slow peers never delay other peers."""

    def __init__(self, websocket, settings: Settings, on_disconnect):
        self.websocket = websocket
        self.settings = settings
        self.on_disconnect = on_disconnect
        self.queue = asyncio.Queue(maxsize=settings.subscriber_queue_size)
        self.pending_bytes = 0
        self.accepting = True
        self.closed = asyncio.Event()
        self.started = asyncio.Event()
        self._overflow = asyncio.Event()
        self.sent = 0
        self.suppressed_duplicates = 0
        self.suppressed_replays = 0
        self._seen = OrderedDict()
        self.task = asyncio.create_task(self._run(), name="live-subscriber")

    def enqueue(self, text: str, *, source_key=None, generation: int = 0) -> bool:
        if not self.accepting:
            return False
        now = time.monotonic()
        if source_key is not None and self.settings.replay_dedup_size:
            while self._seen and next(iter(self._seen.values()))[1] < now - self.settings.replay_dedup_ttl:
                self._seen.popitem(last=False)
            previous = self._seen.get(source_key)
            if previous is not None:
                # An upstream source ID identifies one comment, whether TikTok
                # repeats it on this connection or after reconnecting. Keep this
                # cache per subscriber so newcomers still receive initial history.
                if previous[0] == generation:
                    self.suppressed_duplicates += 1
                else:
                    self.suppressed_replays += 1
                return True
        size = len(text.encode("utf-8"))
        if self.queue.full() or self.pending_bytes + size > self.settings.subscriber_queue_bytes:
            logger.warning(
                "Closing subscriber after backlog limit: queued=%s pending_bytes=%s next_bytes=%s",
                self.queue.qsize(),
                self.pending_bytes,
                size,
            )
            self.accepting = False
            self._overflow.set()
            if self.started.is_set():
                self.task.cancel()
            return False
        self.pending_bytes += size
        self.queue.put_nowait(PendingMessage(text, size, time.monotonic()))
        if source_key is not None and self.settings.replay_dedup_size:
            self._seen[source_key] = (generation, now)
            self._seen.move_to_end(source_key)
            while len(self._seen) > self.settings.replay_dedup_size:
                self._seen.popitem(last=False)
        return True

    async def _run(self) -> None:
        self.started.set()
        try:
            while True:
                if self._overflow.is_set():
                    return
                item = await self.queue.get()
                try:
                    if time.monotonic() - item.queued_at > self.settings.subscriber_max_age:
                        raise TimeoutError("Subscriber backlog is too old")
                    await asyncio.wait_for(self.websocket.send_text(item.text), self.settings.send_timeout)
                    self.sent += 1
                finally:
                    self.pending_bytes -= item.size
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Closing slow/disconnected subscriber: %s", type(exc).__name__)
        finally:
            self.accepting = False
            while not self.queue.empty():
                self.pending_bytes -= self.queue.get_nowait().size
                self.queue.task_done()
            try:
                await asyncio.wait_for(
                    self.websocket.close(code=1013 if self._overflow.is_set() else 1001),
                    self.settings.send_timeout,
                )
            except Exception:
                pass
            finally:
                self.closed.set()
                await self.on_disconnect()

    async def stop(self, *, drain: bool = False) -> None:
        self.accepting = False
        if not self.task.done():
            await self.started.wait()
        if drain and not self.task.done():
            try:
                await asyncio.wait_for(self.queue.join(), self.settings.drain_timeout)
            except asyncio.TimeoutError:
                pass
        if self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
