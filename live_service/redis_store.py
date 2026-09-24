import logging
import os

import redis
import redis.asyncio as async_redis
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Credentials come only from the deployment environment.
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    password=os.getenv("REDIS_PASSWORD"),
    decode_responses=True,
)


class TagUserVo(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    orderNameId: str
    orderNumber: str
    orderAmounts: str | None

    @classmethod
    def parse_from_redis(cls, json_str: str):
        return cls.model_validate_json(json_str)


class FsBlackRedisVo(BaseModel):
    model_config = ConfigDict(strict=True)
    orderNameId: str
    blackLevel: int = Field(ge=0)
    createdUsers: list[str]

    @classmethod
    def parse_from_redis(cls, json_str: str):
        return cls.model_validate_json(json_str)


def create_async_redis(timeout: float):
    kwargs = dict(redis_client.connection_pool.connection_kwargs)
    kwargs.update(
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "32")),
        retry_on_timeout=False,
    )
    if os.getenv("REDIS_URL"):
        return async_redis.Redis.from_url(
            os.environ["REDIS_URL"],
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            max_connections=kwargs["max_connections"],
            retry_on_timeout=False,
        )
    return async_redis.Redis(**kwargs)
