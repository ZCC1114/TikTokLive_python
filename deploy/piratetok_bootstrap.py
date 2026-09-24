"""Anonymous TikTok bootstrap for the isolated PirateTok experiment.

This does not sign URLs, use login/seed cookies, or call a third party. One
fixed HTTP session obtains its own anonymous cookie through TikTok's register
endpoint and resolves the public room. Callers should reuse the returned cookie
on reconnect, never log or serialize the result, and keep WSS metadata consistent
with ``user_agent``. The dependency ``curl_cffi`` belongs to the experiment venv.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

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


class PirateTokBootstrapError(RuntimeError):
    """A fixed reason and safe metadata, never upstream exception/body text."""

    def __init__(self, reason: str, safe_details: dict[str, Any]):
        self.reason = reason
        self.safe_details = safe_details
        super().__init__(f"anonymous bootstrap: {reason}")


def _numeric(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _new_session(timeout: float):
    from curl_cffi import requests
    from curl_cffi.const import CurlHttpVersion

    return requests.Session(
        impersonate="chrome136",
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        verify=True,
        http_version=CurlHttpVersion.V2_0,
        allow_redirects=False,
    )


def _request(session, method: str, url: str, stage: str, deadline: float, diagnostics: dict, **kwargs):
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
        response = session.request(
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
        retry_after = response.headers.get("retry-after", "")
        if isinstance(retry_after, str) and re.fullmatch(r"[0-9]{1,8}", retry_after):
            details["retry_after_seconds"] = int(retry_after)
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


def register_bootstrap(username: str, timeout: float = 8) -> BootstrapResult:
    """Resolve a public online room with one anonymous registration at most.

    ``timeout`` is the entire bootstrap time budget, not a retry interval or a
    per-request timeout. No seed cookies, proxies, identity rotation, or retries
    are added here. A returned ``cookie`` is a complete sensitive Cookie header.
    """
    diagnostics: dict[str, Any] = {"stages": []}
    if not isinstance(username, str):
        raise PirateTokBootstrapError("invalid_username", diagnostics)
    username = username.strip().removeprefix("@")
    if not USERNAME.fullmatch(username):
        raise PirateTokBootstrapError("invalid_username", diagnostics)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise PirateTokBootstrapError("invalid_timeout", diagnostics)
    started = time.monotonic()
    deadline = started + timeout

    try:
        with _new_session(timeout) as session:
            # Preserve the fixed-session sequence validated in the experiment.
            # A 200 challenge page is permitted here because the register API
            # can still issue an anonymous cookie normally in the same session.
            for stage, url in (
                ("profile", f"https://www.tiktok.com/@{username}"),
                ("homepage", "https://www.tiktok.com/"),
            ):
                _request(session, "GET", url, stage, deadline, diagnostics)
            body, stage = _request(
                session, "POST", "https://www.tiktok.com/ttwid/register/", "register", deadline, diagnostics,
                data=json.dumps({
                    "aid": 1988, "service": "www.tiktok.com", "union": False,
                    "unionHost": "", "needFid": False, "fid": "",
                }),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.tiktok.com", "Referer": "https://www.tiktok.com/",
                },
            )
            code = _numeric(_json(body, diagnostics).get("status_code"))
            stage["api_code"] = code
            if code != 0:
                raise PirateTokBootstrapError("registration_rejected", diagnostics)
            _cookie_header(session, diagnostics)  # Ensure registration really yielded a usable token.

            body, stage = _request(
                session, "GET", "https://www.tiktok.com/api-live/user/room", "room", deadline, diagnostics,
                params={
                    "aid": "1988", "app_name": "tiktok_web", "device_platform": "web_pc",
                    "app_language": "en", "browser_language": "en-US", "region": "US",
                    "user_is_login": "false", "sourceType": "54", "staleTime": "600000", "uniqueId": username,
                },
            )
            payload = _json(body, diagnostics)
            code = _numeric(payload.get("statusCode"))
            stage["api_code"] = code
            if code == 19881007:
                raise PirateTokBootstrapError("user_not_found", diagnostics)
            if code == 4003110:
                raise PirateTokBootstrapError("restricted_room", diagnostics)
            if code != 0:
                raise PirateTokBootstrapError("room_rejected", diagnostics)
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
                raise PirateTokBootstrapError("invalid_response", diagnostics)
            user = data["user"]
            live_room = data.get("liveRoom") or {}
            if not isinstance(live_room, dict):
                raise PirateTokBootstrapError("invalid_response", diagnostics)
            live_status = _numeric(live_room.get("status"))
            user_status = _numeric(user.get("status"))
            stage.update({"live_status": live_status, "user_status": user_status})
            room_id = str(user.get("roomId") or "")
            if not room_id or room_id == "0" or (live_status != 2 and user_status != 2):
                raise PirateTokBootstrapError("offline", diagnostics)
            if not re.fullmatch(r"[0-9]{1,20}", room_id):
                raise PirateTokBootstrapError("invalid_response", diagnostics)
            cookie = _cookie_header(session, diagnostics)
            diagnostics["seconds"] = round(time.monotonic() - started, 3)
            return BootstrapResult(room_id, cookie, USER_AGENT, diagnostics)
    except PirateTokBootstrapError:
        diagnostics["seconds"] = round(time.monotonic() - started, 3)
        raise
    except Exception:
        diagnostics["seconds"] = round(time.monotonic() - started, 3)
        raise PirateTokBootstrapError("transport_error", diagnostics) from None
