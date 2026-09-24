"""Keep main's SDK features alongside the unsigned service's lifecycle fixes."""

from unittest.mock import AsyncMock

import httpx
import pytest

from TikTokLive import TikTokLiveClient
from TikTokLive.client.web.routes.fetch_signed_websocket import WebcastPlatform
from TikTokLive.client.web.web_settings import WebDefaults
from TikTokLive.client.web.web_signer import TikTokSigner
from TikTokLive.events import BarrageEvent
from TikTokLive.events.custom_events import SuperFanEvent
from TikTokLive.proto import ProtoMessageFetchResultBaseProtoMessage


@pytest.mark.parametrize("platform", [WebcastPlatform.WEB, WebcastPlatform.MOBILE])
async def test_selected_platform_reaches_signer_with_bounded_retry(platform, monkeypatch):
    client = TikTokLiveClient("creator", platform=platform)
    request = AsyncMock(side_effect=httpx.ReadTimeout("injected"))
    client.web.signer.client.get = request
    if platform is WebcastPlatform.MOBILE:
        monkeypatch.setenv("WHITELIST_AUTHENTICATED_SESSION_ID_HOST", WebDefaults.tiktok_sign_url.split("://")[1])
        client.web.cookies.set("sessionid", "unit-test-session")
        client.web.cookies.set("tt-target-idc", "unit-test-idc")
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.start(room_id=7, fetch_live_check=False, sign_api_retries=0, sign_api_timeout=0.1)
        assert request.await_count == 1
        assert request.call_args.kwargs["params"]["platform"] == platform.value
        assert request.call_args.kwargs["timeout"] == 0.1
    finally:
        await client.disconnect(close_client=True)


async def test_mobile_without_session_does_not_contact_signer():
    client = TikTokLiveClient("creator", platform=WebcastPlatform.MOBILE)
    request = AsyncMock()
    client.web.signer.client.get = request
    try:
        with pytest.raises(ValueError, match="Mobile platform requires"):
            await client.start(room_id=7, fetch_live_check=False)
        request.assert_not_awaited()
    finally:
        await client.disconnect(close_client=True)


@pytest.mark.parametrize("explicit", [False, True])
async def test_signer_environment_overrides_defaults_but_not_explicit_arguments(monkeypatch, explicit):
    monkeypatch.setenv("SIGN_API_KEY", "unit-environment-key")
    monkeypatch.setenv("SIGN_API_URL", "https://environment.invalid")
    monkeypatch.setattr(WebDefaults, "tiktok_sign_api_key", "unit-default-key")
    monkeypatch.setattr(WebDefaults, "tiktok_sign_url", "https://default.invalid")
    kwargs = {"sign_api_key": "unit-explicit-key", "sign_api_base": "https://explicit.invalid"} if explicit else {}
    signer = TikTokSigner(sign_api_timeout=0.5, **kwargs)
    try:
        expected = "explicit" if explicit else "environment"
        assert signer.sign_api_key == f"unit-{expected}-key"
        assert signer._sign_api_base == f"https://{expected}.invalid"
        assert signer.client.timeout.read == 0.5
    finally:
        await signer.client.aclose()


@pytest.mark.parametrize("flag", ["is_user_id", "is_userid"])
async def test_both_user_id_keyword_spellings_resolve_numeric_identifiers(flag):
    client = TikTokLiveClient(123, **{flag: True})
    client.web.fetch_user_unique_id = AsyncMock(return_value="creator")
    try:
        assert await client._resolve_user_id("123") == "creator"
        client.web.fetch_user_unique_id.assert_awaited_once_with(123)
    finally:
        await client.disconnect(close_client=True)


async def test_super_fan_event_survives_service_event_sink_changes():
    client = TikTokLiveClient("creator")
    event = BarrageEvent().from_dict({"content": {"key": "ttlive_superFan"}})
    message = ProtoMessageFetchResultBaseProtoMessage(method="WebcastBarrageMessage", payload=bytes(event))
    try:
        events = await client._parse_webcast_response_message(message)
        assert any(isinstance(parsed, SuperFanEvent) for parsed in events)
    finally:
        await client.disconnect(close_client=True)
