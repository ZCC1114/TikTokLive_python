import json
from typing import Optional, List, Annotated

import redis
from pydantic import BaseModel, Field

redis_client = redis.Redis(host="52.77.231.48", port=6379, password='fastSort8888',decode_responses=True)


class TagUserVo(BaseModel):
    id: Optional[str]
    orderNameId: Optional[str]
    orderNumber: Optional[str]
    orderAmounts: Optional[str]

    @classmethod
    def parse_from_redis(cls, json_str: str):
        try:
            if not json_str or not isinstance(json_str, str):
                return None
            first_pass = json.loads(json_str)
            if isinstance(first_pass, str):
                first_pass = json.loads(first_pass)
            if not isinstance(first_pass, dict):
                raise ValueError("最终解码结果不是 dict")
            return cls.model_validate(first_pass)
        except Exception as e:
            print(f"❌ 标签用户模型解析失败: {e}")
            print(f"❌ 原始字符串: {json_str}")
            return None


class FsBlackRedisVo(BaseModel):
    orderNameId: Optional[str]
    blackLevel: int = 0
    createdUsers: Annotated[List[str], Field(default_factory=list)]

    @classmethod
    def parse_from_redis(cls, json_str: str):
        try:
            raw = json.loads(json_str)
            users = raw.get("createdUsers", [])
            if (
                isinstance(users, list)
                and len(users) == 2
                and isinstance(users[1], list)
            ):
                users_list = users[1]
            else:
                users_list = []
            return cls(
                orderNameId=raw.get("orderNameId"),
                blackLevel=raw.get("blackLevel", 0),
                createdUsers=users_list,
            )
        except Exception as e:
            print(f"❌ FsBlackRedisVo 解析失败: {e}")
            return None
