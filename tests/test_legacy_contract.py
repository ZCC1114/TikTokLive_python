"""Current cross-service JSON contract and bounded failure behavior."""
import asyncio
import json
import uuid

import pytest
from conftest import comment

from live_service.enrichment import RedisEnricher
from live_service.protocol import comment_message, control_status
from TikTokLive.proto.custom_proto import ControlAction


class Pipeline:
    def __init__(self, values, error=None):
        self.values, self.error = values, error
        self.ready = "1"
        self.keys = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, key):
        self.keys.append(key)
        return self

    async def execute(self, raise_on_error=False):
        if self.error:
            raise self.error
        return [*self.values, self.ready]


class AsyncRedis:
    def __init__(self, values):
        self.pipe = Pipeline(values)
        self.calls = 0

    def pipeline(self, transaction=False):
        assert transaction
        self.calls += 1
        return self.pipe

    async def aclose(self):
        pass


TAG = json.dumps(dict(id="1", orderNameId="viewer", orderNumber="A007", orderAmounts="3"))
BLACK = json.dumps(dict(orderNameId="viewer", blackLevel=3, createdUsers=["alice", "bob"]))


async def test_comment_metadata_uses_plain_json_and_native_field_types():
    actual = comment_message(comment(9223372036854775806))
    redis = AsyncRedis([TAG, BLACK])
    await RedisEnricher(timeout=1, cooldown=0, client=redis).enrich(actual)
    assert uuid.UUID(actual["msgId"]).version == 4
    assert actual["dyMsgId"] == "9223372036854775806"
    assert actual["orderNumber"] == "A007"
    assert actual["blackLevel"] == "3"
    assert actual["createdUsers"] == ["alice", "bob"]
    assert actual["metadataAvailable"] is True
    assert redis.pipe.keys == ["orderUser:dy_room_id_user:7300000000000000001:viewer", "black:viewer", "quick_pick:metadata_ready:v1"]


@pytest.mark.parametrize("values", [
    [json.dumps(TAG), BLACK],
    [TAG, json.dumps(dict(orderNameId="viewer", blackLevel=3, createdUsers=["java.util.ArrayList", ["alice"]]))],
    ["invalid JSON", BLACK], [TAG, "invalid JSON"],
    [TAG, json.dumps(dict(blackLevel=3, createdUsers=[]))],
    [TAG.replace('viewer', 'another-buyer'), BLACK],
    [TAG, BLACK.replace('viewer', 'another-buyer')],
    [RuntimeError("first read failed"), BLACK], [TAG, RuntimeError("second read failed")],
])
async def test_old_or_corrupt_metadata_never_becomes_an_allow_decision(values):
    message = comment_message(comment())
    await RedisEnricher(timeout=1, cooldown=0, client=AsyncRedis(values)).enrich(message)
    assert message["metadataAvailable"] is False
    assert "orderNumber" not in message and "blackLevel" not in message and "createdUsers" not in message
    assert message["danmuContent"] == "你好 🌷"


async def test_missing_keys_are_valid_only_after_business_projection_is_ready():
    message = comment_message(comment())
    await RedisEnricher(timeout=1, cooldown=0, client=AsyncRedis([None, None])).enrich(message)
    assert message["metadataAvailable"] is True
    assert message["createdUsers"] == []
    assert message["blackLevel"] == "0"
    assert message["orderNumber"] == ""


@pytest.mark.parametrize("values", [[None, None], [TAG, BLACK]])
@pytest.mark.parametrize("ready", [None, "0", RuntimeError("readiness unavailable")])
async def test_lost_or_rebuilding_cache_never_authorizes_automatic_printing(values, ready):
    redis = AsyncRedis(values)
    redis.pipe.ready = ready
    message = comment_message(comment())
    await RedisEnricher(timeout=1, cooldown=0, client=redis).enrich(message)
    assert message["metadataAvailable"] is False
    assert "orderNumber" not in message and "blackLevel" not in message


@pytest.mark.parametrize("action, expected", [(1, "1"), (2, "2"), (3, "3"), (4, "3")])
def test_control_status(action, expected):
    assert control_status(ControlAction(action)) == expected


async def test_redis_timeout_keeps_original_fields_and_does_not_block_loop():
    redis = AsyncRedis([None, None])

    async def stalled(**kwargs):
        await asyncio.sleep(30)

    redis.pipe.execute = stalled
    enricher = RedisEnricher(timeout=0.02, cooldown=1, client=redis)
    message = comment_message(comment())
    before = dict(message)
    tick = asyncio.Event()
    asyncio.get_running_loop().call_later(0.001, tick.set)
    await enricher.enrich(message)
    assert tick.is_set()
    assert message == {**before, "metadataAvailable": False}
    await enricher.enrich(message)
    assert redis.calls == 1
    assert enricher.failures == 1 and enricher.degraded == 2


async def test_labels_are_not_cached_between_comments():
    redis = AsyncRedis([TAG, BLACK])
    enricher = RedisEnricher(timeout=1, cooldown=0, client=redis)
    first, second = comment_message(comment(1)), comment_message(comment(2))
    await enricher.enrich(first)
    redis.pipe.values = [None, None]
    await enricher.enrich(second)
    assert first["orderNumber"] == "A007"
    assert second["orderNumber"] == ""
    assert second["createdUsers"] == []
    assert redis.calls == 2


async def test_one_bad_redis_key_does_not_disable_other_users_metadata():
    from redis.exceptions import ResponseError

    redis = AsyncRedis([TAG, ResponseError("WRONGTYPE")])
    enricher = RedisEnricher(timeout=1, cooldown=30, client=redis)
    await enricher.enrich(comment_message(comment()))
    redis.pipe.values = [TAG, BLACK]
    healthy = comment_message(comment(2))
    await enricher.enrich(healthy)
    assert healthy["orderNumber"] == "A007" and healthy["blackLevel"] == "3"
