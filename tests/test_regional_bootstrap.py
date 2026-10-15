import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from live_service.upstream.bootstrap import AnonymousBootstrap, BootstrapResult
from live_service.upstream.errors import UpstreamError
from live_service.upstream.regional_bootstrap import RegionalBootstrap, build_bootstrap


def result(cookie="direct-cookie", room_id="123", live=True):
    return BootstrapResult(
        room_id, cookie, "test-agent", {"stages": []},
        {"username": "creator", "nickname": "Creator", "avatarUrl": "",
         "roomId": room_id if live else "", "live": live},
    )


def provider():
    return Mock(resolve=AsyncMock(), close=AsyncMock(), snapshot=Mock(return_value={}))


class RegionalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.direct, self.regional = provider(), provider()
        self.bootstrap = RegionalBootstrap(self.direct, self.regional, timeout=0.2)

    async def test_direct_success_does_not_call_regional(self):
        self.direct.resolve.return_value = result()
        value = await self.bootstrap.resolve("creator")
        self.assertEqual(value.cookie, "direct-cookie")
        self.regional.resolve.assert_not_awaited()

    async def test_regional_identity_uses_direct_cookie(self):
        self.direct.resolve.side_effect = [UpstreamError("user_not_found"), result()]
        self.regional.resolve.return_value = result("regional-private-cookie")
        value = await self.bootstrap.resolve("creator", require_live=False)
        self.assertEqual(value.cookie, "direct-cookie")
        self.assertEqual(value.profile["nickname"], "Creator")
        self.assertEqual(value.diagnostics["lookup_route"], "regional_fallback")
        self.assertNotIn("regional-private-cookie", repr(value))
        self.assertNotIn("cookie", json.dumps(value.diagnostics))
        self.direct.resolve.assert_awaited_with("creator", room_id=123, require_live=False)

    async def test_offline_profile_can_be_resolved_without_faking_live(self):
        self.direct.resolve.side_effect = [UpstreamError("user_not_found"), result(room_id="0", live=False)]
        self.regional.resolve.return_value = result("regional-cookie", room_id="0", live=False)
        value = await self.bootstrap.resolve("creator", require_live=False)
        self.assertFalse(value.profile["live"])
        self.assertEqual(value.room_id, "0")

    async def test_offline_cannot_open_live_connection(self):
        self.direct.resolve.side_effect = UpstreamError("user_not_found")
        self.regional.resolve.side_effect = UpstreamError("offline", terminal=True, refresh_room=True)
        with self.assertRaises(UpstreamError) as caught:
            await self.bootstrap.resolve("creator")
        self.assertEqual(caught.exception.reason, "offline")
        self.assertTrue(caught.exception.refresh_room)
        self.assertEqual(self.direct.resolve.await_count, 1)

    async def test_restrictions_rejections_timeouts_and_rates_never_change_egress(self):
        for reason in ["restricted_room", "rate_limited", "bootstrap_rejected",
                       "bootstrap_transport_error", "bootstrap_timeout", "offline"]:
            with self.subTest(reason=reason):
                error = UpstreamError(reason, retry_after=60)
                self.direct.resolve.side_effect = error
                with self.assertRaises(UpstreamError) as caught:
                    await self.bootstrap.resolve("creator")
                self.assertIs(caught.exception, error)
                self.regional.resolve.assert_not_awaited()

    async def test_cached_room_does_not_trigger_regional(self):
        self.direct.resolve.side_effect = UpstreamError("user_not_found")
        with self.assertRaises(UpstreamError):
            await self.bootstrap.resolve("creator", room_id=123)
        self.regional.resolve.assert_not_awaited()

    async def test_regional_rate_limit_preserves_retry_after(self):
        self.direct.resolve.side_effect = UpstreamError("user_not_found")
        self.regional.resolve.side_effect = UpstreamError("rate_limited", retry_after=60)
        with self.assertRaises(UpstreamError) as caught:
            await self.bootstrap.resolve("creator")
        self.assertEqual(caught.exception.retry_after, 60)
        self.assertEqual(caught.exception.cooldown_scope, "regional")
        self.assertEqual(self.direct.resolve.await_count, 1)

    async def test_regional_rate_limit_does_not_pause_unrelated_rooms(self):
        from live_service.manager import ConnectionManager
        manager = ConnectionManager.__new__(ConnectionManager)
        manager._upstream_not_before = 0.0
        error = UpstreamError("rate_limited", retry_after=60, cooldown_scope="regional")
        self.assertEqual(manager._retry_delay(error, 0), 60)
        self.assertEqual(manager._upstream_not_before, 0.0)

    async def test_default_rate_limit_still_pauses_default_egress(self):
        from live_service.manager import ConnectionManager
        manager = ConnectionManager.__new__(ConnectionManager)
        manager._upstream_not_before = 0.0
        error = UpstreamError("rate_limited", retry_after=60)
        self.assertEqual(manager._retry_delay(error, 0), 60)
        self.assertGreater(manager._upstream_not_before, 0.0)

    async def test_regional_transport_failure_is_not_misreported_as_missing_user(self):
        self.direct.resolve.side_effect = UpstreamError("user_not_found")
        self.regional.resolve.side_effect = UpstreamError("bootstrap_transport_error")
        with self.assertRaises(UpstreamError) as caught:
            await self.bootstrap.resolve("creator")
        self.assertEqual(caught.exception.reason, "bootstrap_transport_error")
        self.assertFalse(caught.exception.terminal)

    async def test_total_timeout_cancels_regional_request(self):
        cancelled = asyncio.Event()
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        self.direct.resolve.side_effect = UpstreamError("user_not_found")
        self.regional.resolve.side_effect = slow
        self.bootstrap.timeout = 0.01
        with self.assertRaises(UpstreamError) as caught:
            await self.bootstrap.resolve("creator")
        self.assertEqual(caught.exception.reason, "bootstrap_timeout")
        self.assertTrue(cancelled.is_set())

    async def test_caller_cancellation_propagates(self):
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.sleep(10)
        self.direct.resolve.side_effect = UpstreamError("user_not_found")
        self.regional.resolve.side_effect = slow
        task = asyncio.create_task(self.bootstrap.resolve("creator"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_close_both_sessions_even_if_direct_close_fails(self):
        self.direct.close.side_effect = RuntimeError("closed")
        with self.assertRaises(RuntimeError):
            await self.bootstrap.close()
        self.regional.close.assert_awaited_once()

    async def test_failure_is_not_cached_next_resolution_checks_again(self):
        self.direct.resolve.side_effect = [UpstreamError("user_not_found"), UpstreamError("user_not_found"), result()]
        self.regional.resolve.side_effect = [UpstreamError("offline"), result("regional-cookie")]
        with self.assertRaises(UpstreamError):
            await self.bootstrap.resolve("creator")
        self.assertEqual((await self.bootstrap.resolve("creator")).room_id, "123")
        self.assertEqual(self.regional.resolve.await_count, 2)


class FactoryTests(unittest.TestCase):
    def test_unconfigured_factory_preserves_original_bootstrap(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(build_bootstrap(), AnonymousBootstrap)

    def test_invalid_configuration_does_not_echo_secrets(self):
        for url in ["file://private-secret", "https://user:private-secret@host:bad",
                    "https://user:private-secret@host:443/path", "https://host:443/#private-secret"]:
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                build_bootstrap(proxy_url=url)
            self.assertNotIn("private-secret", str(caught.exception))

    def test_proxy_is_isolated_to_regional_http_session(self):
        with patch("live_service.upstream.regional_bootstrap._new_session", return_value=Mock()) as create:
            value = build_bootstrap(proxy_url="http://127.0.0.1:12345")
            self.assertIsInstance(value, RegionalBootstrap)
            create.assert_not_called()
            session = value.regional._factory(8)
            self.assertEqual(session.proxies["https"], "http://127.0.0.1:12345")
            self.assertIsNone(value.direct._session)


if __name__ == "__main__":
    unittest.main()
