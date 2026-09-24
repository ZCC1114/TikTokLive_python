"""Quick Pick barrage wire contract shared with the native iOS and Android apps."""

from __future__ import annotations

import json
import logging
import uuid

from TikTokLive.proto.custom_proto import ControlAction

logger = logging.getLogger(__name__)


def control_status(action: ControlAction) -> str:
    return {
        ControlAction.CONTROL_ACTION_STREAM_ENDED: "3",
        ControlAction.CONTROL_ACTION_STREAM_SUSPENDED: "3",
        ControlAction.CONTROL_ACTION_STREAM_PAUSED: "1",
        ControlAction.CONTROL_ACTION_STREAM_UNPAUSED: "2",
    }.get(action, "0")


def comment_message(event, *, debug_raw: bool = False) -> dict:
    if debug_raw:
        try:
            raw = event.to_dict() if hasattr(event, "to_dict") else vars(event)
            logger.info("RAW_COMMENT_EVENT_JSON=%s", json.dumps(raw, ensure_ascii=False, default=str))
            if getattr(event, "bytes", None):
                logger.info("RAW_COMMENT_EVENT_BASE64=%s", event.as_base64)
        except Exception:
            logger.warning("Raw comment logging failed", exc_info=True)
    user = event.user
    return {
        "msgId": str(uuid.uuid4()),
        "dyMsgId": str(event.base_message.message_id),
        "danmuUserId": str(user.unique_id),
        "username": str(user.username or user.unique_id),
        "danmuUserName": str(user.nick_name),
        "danmuContent": str(event.comment),
        "dyRoomId": str(event.base_message.room_id),
        "fansStatus": "0",
    }
