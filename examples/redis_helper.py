import json
from dataclasses import dataclass
from typing import Optional

import redis

redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)


@dataclass
class TagUserVo:
    """Simple representation of tagged user info."""

    orderNumber: str = ""

    @staticmethod
    def parse_from_redis(value: str) -> "TagUserVo":
        try:
            data = json.loads(value)
        except Exception:
            data = {}
        return TagUserVo(orderNumber=data.get("orderNumber", ""))


@dataclass
class FsBlackRedisVo:
    """Simple representation of blacklist info."""

    blackLevel: int = 0
    createdUsers: str = "[]"

    @staticmethod
    def parse_from_redis(value: str) -> "FsBlackRedisVo":
        try:
            data = json.loads(value)
        except Exception:
            data = {}
        return FsBlackRedisVo(
            blackLevel=int(data.get("blackLevel", 0)),
            createdUsers=data.get("createdUsers", "[]"),
        )
