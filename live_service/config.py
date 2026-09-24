from __future__ import annotations

import math
import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Settings:
    connect_timeout: float = 12.0
    connect_concurrency: int = 4
    max_rooms: int = 50
    max_subscribers: int = 1000
    max_subscribers_per_room: int = 200
    fast_reconnect_timeout: float = 5.0
    fast_reconnect_delay: float = 0.2
    room_id_cache_ttl: float = 60.0
    bootstrap_timeout: float = 8.0
    anonymous_cookie_ttl: float = 900.0
    upstream_open_timeout: float = 3.0
    upstream_heartbeat_interval: float = 3.0
    upstream_idle_timeout: float = 8.0
    upstream_send_timeout: float = 2.0
    retry_initial: float = 1.0
    retry_max: float = 30.0
    retry_reset_after: float = 30.0
    idle_grace: float = 0.0
    room_queue_size: int = 1024
    subscriber_queue_size: int = 512
    subscriber_queue_bytes: int = 4 * 1024 * 1024
    subscriber_max_age: float = 30.0
    replay_dedup_size: int = 4096
    replay_dedup_ttl: float = 3600.0
    send_timeout: float = 5.0
    cleanup_timeout: float = 10.0
    drain_timeout: float = 5.0
    redis_timeout: float = 1.0
    redis_failure_cooldown: float = 2.0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            minimum = 0 if field.name in {"idle_grace", "redis_failure_cooldown", "replay_dedup_size"} else 0.000001
            if not math.isfinite(value) or value < minimum:
                raise ValueError(f"{field.name} must be finite and >= {minimum}")
        if self.retry_max < self.retry_initial:
            raise ValueError("retry_max must be >= retry_initial")
        if self.upstream_idle_timeout <= self.upstream_heartbeat_interval:
            raise ValueError("upstream_idle_timeout must exceed upstream_heartbeat_interval")

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        values = {}
        for field in fields(cls):
            key = f"LIVE_{field.name.upper()}"
            if key in os.environ:
                values[field.name] = type(getattr(defaults, field.name))(os.environ[key])
        return cls(**values)
