"""Asynchronous anonymous TikTok session registration and public room discovery.

This does not sign URLs, use login/seed cookies, or call a third party. One
fixed HTTP session obtains its own anonymous cookie through TikTok's register
endpoint and resolves the public room. Callers should reuse the returned cookie
on reconnect, never log or serialize the result, and keep WSS metadata consistent
with ``user_agent``. HTTP requests are bounded and cancellation releases their curl handles.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from live_service.upstream.errors import UpstreamError, retry_after_seconds

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_COOKIE_BYTES = 16 * 1024
USERNAME = re.compile(r"[A-Za-z0-9_.]{1,64}\Z")
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
COOKIE_VALUE = re.compile(r"[\x21-\x3a\x3c-\x7e]+\Z")


@dataclass(frozen=True)
class BootstrapResult:
    room_id: str
    cookie: str = field(repr=False)
    user_agent: str
    diagnostics: dict[str, Any]
    profile: dict[str, Any] | None = None


class PirateTokBootstrapError(RuntimeError):
    """A fixed reason and safe metadata, never upstream exception/body text."""

    def __init__(self, reason: str, safe_details: dict[str, Any]):
        self.reason = reason
        self.safe_details = safe_details
        super().__init__(f"anonymous bootstrap: {reason}")


def _numeric(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _new_session(timeout: float):
    from curl_cffi.const import CurlHttpVersion
    from curl_cffi.requests import AsyncSession

    return AsyncSession(max_clients=4,
        impersonate="chrome136",
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        verify=True,
        http_version=CurlHttpVersion.V2_0,
        allow_redirects=False,
    )


async def _request(session, method: str, url: str, stage: str, deadline: float, diagnostics: dict, **kwargs):
    started = time.monotonic()
    remaining = deadline - started
    details: dict[str, Any] = {"stage": stage}
    diagnostics["stages"].append(details)
    if remaining <= 0:
        raise PirateTokBootstrapError("timeout", diagnostics)
    content = bytearray()
    oversized = False

    def collect(chunk: bytes) -> int:
        nonlocal oversized
        if len(content) + len(chunk) > MAX_BODY_BYTES:
            oversized = True
            # curl_cffi 0.13 ignores a short write return (including zero).
            # libcurl's CURL_WRITEFUNC_ERROR is the explicit abort sentinel.
            return 0xFFFFFFFF
        content.extend(chunk)
        return len(chunk)

    try:
        response = await session.request(
            method, url, timeout=remaining, verify=True, allow_redirects=False,
            content_callback=collect, **kwargs,
        )
    except Exception:
        details["seconds"] = round(time.monotonic() - started, 3)
        details["body_bytes"] = len(content)
        if oversized:
            raise PirateTokBootstrapError("body_too_large", diagnostics) from None
        reason = "timeout" if time.monotonic() >= deadline else "transport_error"
        raise PirateTokBootstrapError(reason, diagnostics) from None

    details.update({
        "seconds": round(time.monotonic() - started, 3),
        "http_status": _numeric(response.status_code),
        "http_version": _numeric(response.http_version),
        "body_bytes": len(content),
    })
    system_error = response.headers.get("x-tt-system-error", "")
    if isinstance(system_error, str) and re.fullmatch(r"[0-9]{1,10}", system_error):
        details["tt_system_error"] = int(system_error)
    if response.status_code == 429:
        details["retry_after_seconds"] = retry_after_seconds(response.headers.get("retry-after"))
        raise PirateTokBootstrapError("rate_limited", diagnostics)
    if response.status_code in (401, 403):
        raise PirateTokBootstrapError("rejected", diagnostics)
    if 300 <= response.status_code < 400:
        # The successful path needs no redirect. Never follow an untrusted host
        # or echo a Location URL into diagnostics.
        raise PirateTokBootstrapError("redirect_rejected", diagnostics)
    if response.status_code != 200:
        raise PirateTokBootstrapError("http_error", diagnostics)
    return bytes(content), details


def _json(body: bytes, diagnostics: dict) -> dict:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise PirateTokBootstrapError("invalid_response", diagnostics) from None
    if not isinstance(value, dict):
        raise PirateTokBootstrapError("invalid_response", diagnostics)
    return value


def _cookie_header(session, diagnostics: dict) -> str:
    # Only normal domain cookies from this fresh session can reach the webcast
    # sibling host. Host-only www.tiktok.com cookies must stay on that host.
    cookies = {}
    for cookie in session.cookies.jar:
        if cookie.domain not in (".tiktok.com", "tiktok.com") or not cookie.domain_specified:
            continue
        if cookie.path != "/" or cookie.is_expired():
            continue
        if not COOKIE_NAME.fullmatch(cookie.name) or not COOKIE_VALUE.fullmatch(cookie.value):
            raise PirateTokBootstrapError("invalid_cookie", diagnostics)
        if cookie.name in cookies and cookies[cookie.name] != cookie.value:
            raise PirateTokBootstrapError("ambiguous_cookie", diagnostics)
        cookies[cookie.name] = cookie.value
    if not cookies.get("ttwid"):
        raise PirateTokBootstrapError("missing_cookie", diagnostics)
    result = "; ".join(f"{name}={value}" for name, value in cookies.items())
    if len(result) > MAX_COOKIE_BYTES:
        raise PirateTokBootstrapError("cookie_too_large", diagnostics)
    return result


def normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("Invalid TikTok username")
    # Preserve the existing /ws/{username}, @username and pasted LIVE URL forms.
    from urllib.parse import urlsplit

    username = username.strip()
    if username.startswith(("https://", "http://")):
        url = urlsplit(username)
        if url.hostname not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            raise ValueError("Invalid TikTok host")
        username = url.path.lstrip("/").split("/", 1)[0]
    username = username.removeprefix("@")
    if not USERNAME.fullmatch(username) or username in {".", ".."}:
        raise ValueError("Invalid TikTok username")
    return username.lower()


class AnonymousBootstrap:
    """One in-memory anonymous session per service, shared across rooms.

    Registration is single-flight. Reconnects reuse a valid cookie and a room
    explicitly cached by the supervisor; rejection never rotates identity.
    """

    def __init__(self, *, timeout: float = 8, cookie_ttl: float = 900, session_factory=None):
        self.timeout = timeout
        self.cookie_ttl = cookie_ttl
        self._factory = session_factory or _new_session
        self._session = None
        self._lock = asyncio.Lock()
        self._expires = 0.0
        self._closed = False
        self.not_before = 0.0
        self.registrations = 0
        self.room_lookups = 0
        self.cache_hits = 0

    async def resolve(self, username: str, *, room_id: int | None = None, require_live: bool = True) -> BootstrapResult:
        username = normalize_username(username)
        diagnostics: dict[str, Any] = {"stages": []}
        started = time.monotonic()
        try:
            return await asyncio.wait_for(self._resolve(username, room_id, diagnostics, require_live), self.timeout)
        except asyncio.TimeoutError:
            raise UpstreamError("bootstrap_timeout") from None
        except PirateTokBootstrapError as exc:
            if exc.reason == "rate_limited":
                retry = max(1, exc.safe_details["stages"][-1].get("retry_after_seconds", 60))
                self.not_before = max(self.not_before, time.monotonic() + retry)
                raise UpstreamError("rate_limited", retry_after=retry) from None
            terminal = exc.reason not in {"timeout", "transport_error", "http_error"}
            raise UpstreamError(f"bootstrap_{exc.reason}", terminal=terminal) from None
        finally:
            diagnostics["seconds"] = round(time.monotonic() - started, 3)

    async def _resolve(self, username, room_id, diagnostics, require_live):
        deadline = time.monotonic() + self.timeout
        async with self._lock:
            if self._closed:
                raise UpstreamError("bootstrap_closed", terminal=True)
            if time.monotonic() < self.not_before:
                raise UpstreamError("rate_limited", retry_after=self.not_before - time.monotonic())
            if self._session is None:
                self._session = self._factory(self.timeout)
            session = self._session
            if time.monotonic() >= self._expires:
                body, stage = await _request(
                    session, "POST", "https://www.tiktok.com/ttwid/register/", "register", deadline, diagnostics,
                    data=json.dumps({"aid": 1988, "service": "www.tiktok.com", "union": False,
                                     "unionHost": "", "needFid": False, "fid": ""}),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://www.tiktok.com",
                             "Referer": "https://www.tiktok.com/"},
                )
                stage["api_code"] = _numeric(_json(body, diagnostics).get("status_code"))
                if stage["api_code"] != 0:
                    raise PirateTokBootstrapError("registration_rejected", diagnostics)
                _cookie_header(session, diagnostics)
                self.registrations += 1
                # Never keep a cache longer than the cookie's actual expiration.
                ttl = self.cookie_ttl
                for item in session.cookies.jar:
                    if item.name == "ttwid" and item.expires is not None:
                        ttl = min(ttl, max(0, item.expires - time.time() - 5))
                self._expires = time.monotonic() + ttl
                diagnostics["cookie_cached"] = False
            else:
                diagnostics["cookie_cached"] = True
                self.cache_hits += 1
            cookie = _cookie_header(session, diagnostics)

        profile = None
        if room_id is None:
            self.room_lookups += 1
            body, stage = await _request(
                session, "GET", "https://www.tiktok.com/api-live/user/room", "room", deadline, diagnostics,
                params={"aid": "1988", "app_name": "tiktok_web", "device_platform": "web_pc", "app_language": "en",
                        "browser_language": "en-US", "region": "US", "user_is_login": "false", "sourceType": "54",
                        "staleTime": "600000", "uniqueId": username},
            )
            payload = _json(body, diagnostics)
            code = _numeric(payload.get("statusCode"))
            stage["api_code"] = code
            if code in {19881007, 4003110}:
                raise UpstreamError("user_not_found" if code == 19881007 else "restricted_room", terminal=True)
            if code != 0:
                raise UpstreamError("room_rejected", terminal=True)
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
                raise PirateTokBootstrapError("invalid_response", diagnostics)
            live = data.get("liveRoom") or {}
            if not isinstance(live, dict):
                raise PirateTokBootstrapError("invalid_response", diagnostics)
            user = data["user"]
            value = str(user.get("roomId") or "")
            is_live = value not in {"", "0"} and (live.get("status") == 2 or user.get("status") == 2)
            if not is_live and require_live:
                raise UpstreamError("offline", terminal=True, refresh_room=True)
            if is_live and (not re.fullmatch(r"[0-9]{1,20}", value) or not 0 < int(value) < 2**63):
                raise PirateTokBootstrapError("invalid_response", diagnostics)
            if not require_live:
                # This is the single current TikTok room API schema. Never infer
                # live status or creator identity from HTML titles or old aliases.
                nickname, avatar = user.get("nickname"), user.get("avatarThumb")
                status = live.get("status", user.get("status"))
                if (not isinstance(nickname, str) or not nickname.strip() or not isinstance(avatar, str)
                        or type(status) is not int or "roomId" not in user):
                    raise PirateTokBootstrapError("invalid_response", diagnostics)
                profile = {"username": username, "roomId": value if is_live else "",
                           "live": is_live, "nickname": nickname, "avatarUrl": avatar}
            room_id = int(value) if is_live else 0
        if isinstance(room_id, bool) or not isinstance(room_id, int) or not 0 <= room_id < 2**63:
            raise UpstreamError("invalid_room", terminal=True)
        if require_live and room_id == 0:
            raise UpstreamError("offline", terminal=True, refresh_room=True)
        diagnostics["room_cached"] = not any(s["stage"] == "room" for s in diagnostics["stages"])
        return BootstrapResult(str(room_id), cookie, USER_AGENT, diagnostics, profile)

    def snapshot(self) -> dict:
        return {"registrations": self.registrations, "room_lookups": self.room_lookups, "cookie_cache_hits": self.cache_hits,
                "cooldown_seconds": round(max(0, self.not_before - time.monotonic()), 3)}

    async def close(self):
        self._closed = True
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()
        self._expires = 0
