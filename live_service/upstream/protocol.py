"""Unsigned webcast framing, using the existing business protobuf definitions.

Wire path and control frames were verified against PirateTok/live-py commit
ee836405ef122d7e2dd3403d99b91cb0d967977e. No SDK signing client is instantiated.
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass
from urllib.parse import urlencode

import betterproto

from live_service.upstream.bootstrap import USER_AGENT
from live_service.upstream.errors import UpstreamError
from TikTokLive.events import CommentEvent, ControlEvent
from TikTokLive.proto import ProtoMessageFetchResultBaseProtoMessage
from TikTokLive.proto.custom_extras import HeartbeatMessage, WebcastImEnterRoomMessage, WebcastPushFrame

MAX_PAYLOAD = 8 * 1024 * 1024


@dataclass(eq=False, repr=False)
class WebcastResponse(betterproto.Message):
    messages: list[ProtoMessageFetchResultBaseProtoMessage] = betterproto.message_field(1)
    internal_ext: bytes = betterproto.bytes_field(5)
    heartbeat_duration: int = betterproto.int64_field(8)
    needs_ack: bool = betterproto.bool_field(9)


def connection_url(room_id: int, heartbeat_seconds: float) -> str:
    params = {
        "version_code": "180800", "device_platform": "web", "cookie_enabled": "true", "screen_width": "1920",
        "screen_height": "1080", "browser_language": "en-US", "browser_platform": "MacIntel",
        "browser_name": "Mozilla", "browser_version": USER_AGENT.removeprefix("Mozilla/"), "browser_online": "true",
        "tz_name": "UTC", "app_name": "tiktok_web", "sup_ws_ds_opt": "1", "update_version_code": "2.0.0",
        "compress": "gzip", "webcast_language": "en", "ws_direct": "1", "aid": "1988", "live_id": "12",
        "app_language": "en", "client_enter": "1", "room_id": str(room_id), "identity": "audience",
        "history_comment_count": "6", "heartbeat_duration": str(int(heartbeat_seconds * 1000)),
        "resp_content_type": "protobuf", "did_rule": "3",
    }
    return "wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/?" + urlencode(params)


def heartbeat(room_id: int, sequence: int) -> bytes:
    return bytes(WebcastPushFrame(payload_encoding="pb", payload_type="hb",
                                 payload=bytes(HeartbeatMessage(room_id=room_id, send_packet_seq_id=sequence))))


def enter_room(room_id: int) -> bytes:
    return bytes(WebcastPushFrame(payload_encoding="pb", payload_type="im_enter_room", payload=bytes(
        WebcastImEnterRoomMessage(room_id=room_id, live_id=12, identity="audience", filter_welcome_msg="0"))))


def ack(log_id: int, internal_ext: bytes) -> bytes:
    return bytes(WebcastPushFrame(payload_encoding="pb", payload_type="ack", log_id=log_id, payload=internal_ext))


def decode_frame(raw: bytes) -> tuple[WebcastPushFrame, WebcastResponse | None]:
    if not isinstance(raw, bytes) or len(raw) > MAX_PAYLOAD:
        raise UpstreamError("invalid_frame", terminal=True)
    try:
        frame = WebcastPushFrame().parse(raw)
        if frame.payload_type != "msg":
            return frame, None
        payload = frame.payload
        if payload.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                payload = stream.read(MAX_PAYLOAD + 1)
        if len(payload) > MAX_PAYLOAD:
            raise UpstreamError("payload_too_large", terminal=True)
        return frame, WebcastResponse().parse(payload)
    except UpstreamError:
        raise
    except Exception:
        raise UpstreamError("malformed_frame", terminal=True) from None


def business_event(message: ProtoMessageFetchResultBaseProtoMessage, room_id: int):
    event_class = {"WebcastChatMessage": CommentEvent, "WebcastControlMessage": ControlEvent}.get(message.method)
    if event_class is None:
        return None
    try:
        event = event_class().parse(message.payload)
        if event.base_message.message_id <= 0 and message.msg_id > 0:
            event.base_message.message_id = message.msg_id
        if event.base_message.room_id <= 0:
            event.base_message.room_id = room_id
        if event.base_message.room_id != room_id:
            raise UpstreamError("room_mismatch", terminal=True)
        return event
    except UpstreamError:
        raise
    except Exception:
        raise UpstreamError("malformed_business_event", terminal=True) from None
