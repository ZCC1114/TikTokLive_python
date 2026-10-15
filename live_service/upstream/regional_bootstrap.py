"""Optional regional room lookup; the WebSocket keeps its direct session.

Only an API user_not_found can use the configured regional egress. HTTP
rejections, access restrictions and rate limits retain their original meaning.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from urllib.parse import urlsplit

from live_service.upstream.bootstrap import AnonymousBootstrap, _new_session
from live_service.upstream.errors import UpstreamError


class RegionalBootstrap:
    def __init__(self, direct, regional, *, timeout: float):
        self.direct = direct
        self.regional = regional
        self.timeout = timeout
        self.attempts = 0
        self.successes = 0

    async def resolve(self, username, *, room_id=None, require_live=True):
        try:
            return await asyncio.wait_for(
                self._resolve(username, room_id, require_live), self.timeout
            )
        except asyncio.TimeoutError:
            raise UpstreamError("bootstrap_timeout") from None

    async def _resolve(self, username, room_id, require_live):
        try:
            return await self.direct.resolve(
                username, room_id=room_id, require_live=require_live
            )
        except UpstreamError as error:
            if error.reason != "user_not_found" or room_id is not None:
                raise

        self.attempts += 1
        try:
            regional = await self.regional.resolve(username, require_live=require_live)
        except UpstreamError as error:
            # The regional session retains Retry-After; unrelated rooms using
            # the default egress must not inherit this regional cooldown.
            raise UpstreamError(
                error.reason, terminal=error.terminal, retry_after=error.retry_after,
                refresh_room=error.refresh_room, cooldown_scope="regional",
            ) from None
        # A regional lookup supplies identity/live state, never a fabricated or
        # persisted room ID. The direct session still owns the WSS cookie.
        direct = await self.direct.resolve(
            username, room_id=int(regional.room_id), require_live=require_live
        )
        self.successes += 1
        return replace(
            regional,
            cookie=direct.cookie,
            user_agent=direct.user_agent,
            diagnostics={
                "lookup_route": "regional_fallback",
                "direct_reason": "user_not_found",
                "regional": regional.diagnostics,
                "direct_session": direct.diagnostics,
            },
        )

    def snapshot(self):
        result = dict(self.direct.snapshot())
        result["regional_fallback"] = {
            "attempts": self.attempts,
            "successes": self.successes,
            "session": self.regional.snapshot(),
        }
        return result

    async def close(self):
        try:
            await self.direct.close()
        finally:
            await self.regional.close()


def build_bootstrap(*, timeout=8, cookie_ttl=900, proxy_url=None):
    if proxy_url is None:
        proxy_url = os.getenv("LIVE_TIKTOK_US_PROXY_URL", "")
    if not proxy_url:
        return AnonymousBootstrap(timeout=timeout, cookie_ttl=cookie_ttl)
    try:
        parsed = urlsplit(proxy_url)
        valid = (
            parsed.scheme in {"http", "https", "socks5", "socks5h"}
            and parsed.hostname and parsed.port
            and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/"}
            and not any(char.isspace() for char in proxy_url)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        # Never interpolate a proxy URL: it can contain credentials.
        raise ValueError("Invalid LIVE_TIKTOK_US_PROXY_URL")

    def regional_session(timeout):
        session = _new_session(timeout)
        session.proxies = {"http": proxy_url, "https": proxy_url}
        return session

    return RegionalBootstrap(
        AnonymousBootstrap(timeout=timeout, cookie_ttl=cookie_ttl),
        AnonymousBootstrap(
            timeout=timeout, cookie_ttl=cookie_ttl,
            session_factory=regional_session,
        ),
        timeout=timeout,
    )
