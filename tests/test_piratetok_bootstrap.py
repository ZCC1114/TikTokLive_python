"""Bootstrap contracts without TikTok traffic or real credentials."""

import contextlib
import json
import threading
import time
from http.cookiejar import Cookie, CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from deploy import piratetok_bootstrap as bootstrap

TOKEN = "anonymous-test-only-never-log"


def cookie(name="ttwid", domain=".tiktok.com", *, specified=True, path="/", expires=None):
    return Cookie(
        version=0, name=name, value=TOKEN, port=None, port_specified=False,
        domain=domain, domain_specified=specified, domain_initial_dot=domain.startswith("."),
        path=path, path_specified=True, secure=True, expires=expires, discard=expires is None,
        comment=None, comment_url=None, rest={},
    )


def room_reply(*, room_id="123456789", status=2, code=0):
    return {"body": {
        "statusCode": code,
        "data": {"user": {"roomId": room_id, "status": status}, "liveRoom": {"status": status}},
    }}


def successful_replies():
    return [
        {"body": b"challenge page", "headers": {"x-tt-system-error": "3"}},
        {"body": b"challenge page"},
        {"body": {"status_code": 0}, "cookies": [cookie()]},
        room_reply(),
    ]


class Session:
    """A cookie jar and bounded response stream; no network is available."""

    def __init__(self, replies, clock=None):
        self.replies = list(replies)
        self.clock = clock
        self.calls = []
        self.cookies = SimpleNamespace(jar=CookieJar())
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        reply = self.replies.pop(0)
        if self.clock is not None:
            elapsed = reply.get("elapsed", 0)
            self.clock[0] += min(elapsed, kwargs["timeout"])
            if elapsed >= kwargs["timeout"]:
                raise TimeoutError(f"sensitive upstream URL?cookie={TOKEN}")
        for item in reply.get("cookies", []):
            self.cookies.jar.set_cookie(item)
        body = reply.get("body", b"")
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        # Model curl_cffi's actual callback behavior: zero/short writes do not
        # abort; only libcurl's explicit WRITEFUNC_ERROR sentinel does.
        for offset in range(0, len(body), 16384):
            if kwargs["content_callback"](body[offset:offset + 16384]) == 0xFFFFFFFF:
                raise OSError(f"write failed: {TOKEN}")
        return SimpleNamespace(
            status_code=reply.get("status", 200), http_version=3, headers=reply.get("headers", {}),
        )


def use_session(monkeypatch, replies, clock=None):
    session = Session(replies, clock)
    monkeypatch.setattr(bootstrap, "_new_session", lambda timeout: session)
    return session


def test_challenge_pages_can_bootstrap_once_without_exposing_the_cookie(monkeypatch):
    session = use_session(monkeypatch, successful_replies())
    result = bootstrap.register_bootstrap("@public_host")
    assert result.room_id == "123456789"
    assert result.cookie == f"ttwid={TOKEN}"
    assert TOKEN not in repr(result) + json.dumps(result.diagnostics)
    assert session.closed
    assert sum(method == "POST" for method, _, _ in session.calls) == 1
    assert all(url.startswith("https://www.tiktok.com/") for _, url, _ in session.calls)
    assert all(options["verify"] and not options["allow_redirects"] for _, _, options in session.calls)


def test_cookie_bridge_excludes_host_only_wrong_domain_wrong_path_and_expired_cookies(monkeypatch):
    replies = successful_replies()
    replies[2]["cookies"] += [
        cookie("host_only", "tiktok.com", specified=False),
        cookie("www_only", "www.tiktok.com", specified=False),
        cookie("outside", ".other.invalid"),
        cookie("other_path", path="/account"),
        cookie("expired", expires=1),
    ]
    use_session(monkeypatch, replies)
    assert bootstrap.register_bootstrap("public_host").cookie == f"ttwid={TOKEN}"


@pytest.mark.parametrize("invalid", [
    cookie(domain=".other.invalid"),
    cookie(domain="tiktok.com", specified=False),
    cookie(path="/account"),
])
def test_a_ttwid_for_the_wrong_scope_cannot_unlock_wss(monkeypatch, invalid):
    replies = successful_replies()
    replies[2]["cookies"] = [invalid]
    session = use_session(monkeypatch, replies)
    with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
        bootstrap.register_bootstrap("public_host")
    assert caught.value.reason == "missing_cookie"
    assert len(session.calls) == 3 and session.closed


def test_429_stops_without_retry_or_room_lookup_and_preserves_numeric_wait(monkeypatch):
    replies = successful_replies()
    replies[2] = {"status": 429, "headers": {"retry-after": "53"}, "body": TOKEN.encode()}
    session = use_session(monkeypatch, replies)
    with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
        bootstrap.register_bootstrap("public_host")
    error = caught.value
    assert error.reason == "rate_limited"
    assert error.safe_details["stages"][-1]["retry_after_seconds"] == 53
    assert TOKEN not in str(error) + json.dumps(error.safe_details)
    assert len(session.calls) == 3 and session.closed


@pytest.mark.parametrize("status,reason", [(401, "rejected"), (403, "rejected"), (302, "redirect_rejected")])
def test_denial_or_redirect_does_not_trigger_more_requests(monkeypatch, status, reason):
    session = use_session(monkeypatch, [{
        "status": status, "headers": {"location": f"https://other.invalid/?cookie={TOKEN}"},
    }])
    with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
        bootstrap.register_bootstrap("public_host")
    assert caught.value.reason == reason
    assert TOKEN not in json.dumps(caught.value.safe_details)
    assert len(session.calls) == 1 and session.closed


def test_registration_success_without_cookie_is_not_connection_success(monkeypatch):
    replies = successful_replies()
    replies[2].pop("cookies")
    session = use_session(monkeypatch, replies)
    with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
        bootstrap.register_bootstrap("public_host")
    assert caught.value.reason == "missing_cookie"
    assert len(session.calls) == 3


def test_all_requests_share_one_timeout_budget_and_errors_stay_redacted(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: clock[0])
    replies = successful_replies()
    for reply in replies:
        reply["elapsed"] = 3
    session = use_session(monkeypatch, replies, clock)
    with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
        bootstrap.register_bootstrap("public_host", timeout=8)
    assert caught.value.reason == "timeout"
    assert [options["timeout"] for _, _, options in session.calls] == [8, 5, 2]
    assert caught.value.safe_details["seconds"] == 8
    assert TOKEN not in str(caught.value) + json.dumps(caught.value.safe_details)
    assert session.closed


@pytest.mark.parametrize("reply,reason", [
    (room_reply(room_id="0", status=0), "offline"),
    (room_reply(status=0), "offline"),
    (room_reply(code=4003110), "restricted_room"),
    (room_reply(code=19881007), "user_not_found"),
])
def test_unavailable_or_restricted_room_never_returns_a_connection(monkeypatch, reply, reason):
    replies = successful_replies()
    replies[-1] = reply
    session = use_session(monkeypatch, replies)
    with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
        bootstrap.register_bootstrap("public_host")
    assert caught.value.reason == reason
    assert len(session.calls) == 4 and session.closed


def test_oversized_body_stops_buffering_and_prevents_registration(monkeypatch):
    session = use_session(monkeypatch, [{"body": b"x" * (bootstrap.MAX_BODY_BYTES + 16384)}])
    with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
        bootstrap.register_bootstrap("public_host")
    assert caught.value.reason == "body_too_large"
    assert caught.value.safe_details["stages"][0]["body_bytes"] <= bootstrap.MAX_BODY_BYTES
    assert len(session.calls) == 1 and session.closed


def test_real_curl_transport_honors_body_limit_without_tiktok_access():
    requests = pytest.importorskip("curl_cffi.requests")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(bootstrap.MAX_BODY_BYTES * 2))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                for _ in range(512):
                    self.wfile.write(b"x" * 8192)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with requests.Session() as session:
            details = {"stages": []}
            with pytest.raises(bootstrap.PirateTokBootstrapError) as caught:
                bootstrap._request(
                    session, "GET", f"http://127.0.0.1:{server.server_port}/", "loopback",
                    time.monotonic() + 3, details,
                )
            assert caught.value.reason == "body_too_large"
            assert details["stages"][0]["body_bytes"] <= bootstrap.MAX_BODY_BYTES
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
