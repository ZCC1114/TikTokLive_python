from __future__ import annotations

import asyncio
import logging
import time

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from live_service.redis_store import FsBlackRedisVo, TagUserVo, create_async_redis

logger = logging.getLogger(__name__)


class RedisEnricher:
    """One fresh read per comment; unresolved metadata never authorizes automatic printing."""

    def __init__(self, timeout: float, cooldown: float, client=None):
        self.timeout = timeout
        self.cooldown = cooldown
        self.client = client
        self._unavailable_until = 0.0
        self.failures = 0
        self.degraded = 0
        self._reported_failure = False

    async def enrich(self, message: dict) -> None:
        message["metadataAvailable"] = False
        if time.monotonic() < self._unavailable_until:
            self.degraded += 1
            return
        try:
            if self.client is None:
                self.client = create_async_redis(self.timeout)
            # The readiness lease distinguishes a new buyer from lost Redis data.
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.get(f"orderUser:dy_room_id_user:{message['dyRoomId']}:{message['danmuUserId']}")
                pipe.get(f"black:{message['danmuUserId']}")
                pipe.get("quick_pick:metadata_ready:v1")
                tag_raw, black_raw, ready = await asyncio.wait_for(
                    pipe.execute(raise_on_error=False),
                    timeout=self.timeout,
                )
            if ready != "1":
                self.degraded += 1
                return
            if isinstance(tag_raw, Exception):
                raise tag_raw
            if isinstance(black_raw, Exception):
                raise black_raw
            tag = TagUserVo.parse_from_redis(tag_raw) if tag_raw is not None else None
            black = FsBlackRedisVo.parse_from_redis(black_raw) if black_raw is not None else None
            if ((tag and tag.orderNameId != message["danmuUserId"])
                    or (black and black.orderNameId != message["danmuUserId"])):
                raise ValueError("Metadata identity does not match its key")
            message.update(
                orderNumber=tag.orderNumber if tag else "",
                blackLevel=str(black.blackLevel) if black else "0",
                createdUsers=black.createdUsers if black else [],
                metadataAvailable=True,
            )
            self._reported_failure = False
        except Exception as exc:
            self.failures += 1
            self.degraded += 1
            if isinstance(exc, (RedisConnectionError, RedisTimeoutError, OSError, asyncio.TimeoutError)):
                self._unavailable_until = time.monotonic() + self.cooldown
            # Keep the comment visible, with an explicit unavailable marker.
            # No partial blacklist/order snapshot can authorize printing.
            if not self._reported_failure:
                logger.warning("Redis label lookup failed: %s", type(exc).__name__)
                self._reported_failure = True

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
