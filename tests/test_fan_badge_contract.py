"""Fan-club badges must survive real protobuf decoding and metadata enrichment."""
from types import SimpleNamespace

import pytest
from conftest import comment
from test_legacy_contract import AsyncRedis

from live_service.enrichment import RedisEnricher
from live_service.protocol import comment_message, fan_badge_level
from TikTokLive.events import CommentEvent
from TikTokLive.proto import BadgeStruct, PrivilegeLogExtra


@pytest.mark.parametrize("scene,level,expected", [
    (10, "7", 7), (10, " 12 ", 12), (10, "0", 0), (10, "-1", 0),
    (10, "", 0), (10, "1.5", 0), (10, "2e3", 0), (10, "７", 0),
    (10, "2147483648", 0), (4, "7", 0), (7, "7", 0), (8, "7", 0),
])
def test_badge_type_and_level_survive_protobuf_roundtrip(scene, level, expected):
    event = comment()
    event.user_info.badge_list = [BadgeStruct(badge_scene=scene, log_extra=PrivilegeLogExtra(level=level))]
    payload = comment_message(CommentEvent().parse(bytes(event)))
    assert payload["fansLevel"] == expected
    assert payload["fansStatus"] == ("1" if expected else "0")
    assert payload["danmuContent"] == event.comment


def test_malformed_and_unrelated_badges_do_not_turn_a_buyer_into_a_fan():
    assert fan_badge_level(SimpleNamespace()) == 0
    assert fan_badge_level(SimpleNamespace(badge_list=[None, SimpleNamespace(badge_scene=10)])) == 0
    event = comment()
    event.user_info.badge_list = [
        BadgeStruct(badge_scene=8, log_extra=PrivilegeLogExtra(level="99")),
        BadgeStruct(badge_scene=10, log_extra=PrivilegeLogExtra(level="bad")),
        BadgeStruct(badge_scene=10, log_extra=PrivilegeLogExtra(level="7")),
        BadgeStruct(badge_scene=10, log_extra=PrivilegeLogExtra(level="3")),
    ]
    assert comment_message(event)["fansLevel"] == 7


async def test_business_metadata_cannot_erase_platform_fan_badge():
    event = comment()
    event.user_info.badge_list = [BadgeStruct(badge_scene=10, log_extra=PrivilegeLogExtra(level="7"))]
    payload = comment_message(event)
    await RedisEnricher(timeout=1, cooldown=0, client=AsyncRedis([None, None])).enrich(payload)
    assert payload["fansStatus"] == "1" and payload["fansLevel"] == 7
    assert payload["metadataAvailable"] is True
