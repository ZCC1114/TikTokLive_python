"""Offline review probes for the frozen source at 1ec66be (not the current working tree).

Run with Python 3.11+: python plans/tiktok-live-review-probes.py
Loads selected source definitions via AST and supplies fake I/O dependencies.
Does not import the service, read credentials, or contact TikTok/Redis.
These probes demonstrate failure paths; they are not an integration test suite.
"""
from __future__ import annotations

import __future__
import ast
import asyncio
import contextlib
import json
import logging
from pathlib import Path
import re
import time
import subprocess
from types import SimpleNamespace as NS
import uuid


ROOT = Path(__file__).resolve().parents[1]
RESULTS = {}
LOGGER = logging.getLogger("offline-review")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


def load_class(relative_path, name, namespace):
    source = subprocess.check_output(
        ["git", "show", f"1ec66be:{relative_path}"], cwd=ROOT, text=True,
    )
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    code = compile(
        ast.Module(body=[node], type_ignores=[]),
        str(ROOT / relative_path),
        "exec",
        flags=__future__.annotations.compiler_flag,
    )
    exec(code, namespace)
    return namespace[name]


class Socket:
    def __init__(self, delay=0):
        self.messages = []
        self.delay = delay
        self.concurrent_sends = 0
        self.max_concurrent_sends = 0

    async def accept(self):
        pass

    async def send_text(self, message):
        self.concurrent_sends += 1
        self.max_concurrent_sends = max(self.max_concurrent_sends, self.concurrent_sends)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.messages.append((message, time.monotonic()))
        finally:
            self.concurrent_sends -= 1


class FakeClient:
    instances = []

    def __init__(self, unique_id):
        self.handlers = {}
        self.future = asyncio.get_running_loop().create_future()
        self.owner = asyncio.current_task()
        self.instances.append(self)

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback
        return register

    async def start(self):
        return self.future

    async def disconnect(self, close_client=False):
        if not self.future.done():
            self.future.set_result(None)


class FakeRedis:
    def __init__(self, delay=0, fail=False):
        self.delay = delay
        self.fail = fail

    def get(self, key):
        if self.delay:
            time.sleep(self.delay)  # Intentional simulation of blocking I/O.
        if self.fail:
            raise OSError("simulated Redis failure")
        return None


CONNECT, CONTROL, COMMENT = (type(name, (), {}) for name in ("Connect", "Control", "Comment"))
ENV = dict(
    asyncio=asyncio, json=json, uuid=uuid, contextlib=contextlib,
    os=NS(getenv=lambda name, default=None: default), logger=LOGGER,
    httpx=NS(ReadTimeout=type("ReadTimeout", (Exception,), {})),
    WebDefaults=NS(), TikTokLiveClient=FakeClient,
    ConnectEvent=CONNECT, ControlEvent=CONTROL, CommentEvent=COMMENT,
    redis_client=FakeRedis(),
)
Manager = load_class("examples/fastapi_ws_server.py", "ConnectionManager", ENV)


async def finish_clients(instances):
    for client in instances:
        await client.disconnect()
    await asyncio.gather(*(c.owner for c in instances), return_exceptions=True)


async def probe_duplicate_collectors():
    start = len(FakeClient.instances)
    manager = Manager()
    await asyncio.gather(manager.connect(Socket(), "same-room"), manager.connect(Socket(), "same-room"))
    await asyncio.sleep(0)
    created = FakeClient.instances[start:]
    RESULTS["same_room_concurrent_join"] = {
        "collectors_created": len(created), "tracked_tasks": len(manager.tasks)
    }
    assert len(created) == 2 and len(manager.tasks) == 1
    await finish_clients(created)


async def probe_stale_state():
    manager, first = Manager(), Socket()
    await manager.connect(first, "room")
    await asyncio.sleep(0)
    client = FakeClient.instances[-1]
    await client.handlers[CONNECT](None)
    await finish_clients([client])
    stale = manager.live_connected.get("room")
    no_automatic_restart = "room" not in manager.tasks and "room" not in manager.clients
    new_socket = Socket()
    await manager.connect(new_socket, "room")
    await asyncio.sleep(0)
    RESULTS["collector_end"] = {
        "live_connected_still_true": stale,
        "no_restart_task": no_automatic_restart,
        "new_viewer_initial_status": new_socket.messages[0][0],
    }
    assert stale and no_automatic_restart and new_socket.messages[0][0] == "LIVING"
    await finish_clients([FakeClient.instances[-1]])


async def probe_broadcast():
    manager = Manager()
    slow, fast = Socket(delay=0.12), Socket()
    # Use a deterministic iteration order to exercise one valid set ordering.
    manager.active_connections["room"] = [slow, fast]
    started = time.monotonic()
    await manager.broadcast("room", "comment")
    delay_ms = (fast.messages[0][1] - started) * 1000
    concurrent = Socket(delay=0.01)
    manager.active_connections["room"] = [concurrent]
    await asyncio.gather(manager.broadcast("room", "one"), manager.broadcast("room", "two"))
    RESULTS["broadcast"] = {
        "injected_slow_send_ms": 120,
        "healthy_viewer_delivery_ms": round(delay_ms, 1),
        "concurrent_writers_to_one_socket": concurrent.max_concurrent_sends,
    }
    assert delay_ms >= 100 and concurrent.max_concurrent_sends == 2


async def probe_redis_callback():
    manager, socket = Manager(), Socket()
    await manager.connect(socket, "room")
    await asyncio.sleep(0)
    client = FakeClient.instances[-1]
    event = NS(base_message=NS(message_id=42, room_id=7),
               user=NS(unique_id="tester", username="tester", nick_name="Test"), comment="hello")
    ENV["redis_client"] = FakeRedis(delay=0.05)
    loop, started = asyncio.get_running_loop(), time.monotonic()
    fired = loop.create_future()
    loop.call_later(0.005, lambda: fired.set_result(time.monotonic()))
    await client.handlers[COMMENT](event)
    timer_at = await fired
    lag_ms = (timer_at - started - 0.005) * 1000
    ENV["redis_client"] = FakeRedis(fail=True)
    await client.handlers[COMMENT](event)
    payload = json.loads(socket.messages[-1][0])
    missing = [key for key in ("orderNumber", "blackLevel", "createdUsers") if key not in payload]
    RESULTS["redis_callback"] = {
        "injected_get_latency_ms": 50, "get_calls_per_comment": 2,
        "event_loop_timer_lag_ms": round(lag_ms, 1),
        "missing_fields_after_redis_failure": missing,
    }
    assert lag_ms >= 80 and len(missing) == 3
    ENV["redis_client"] = FakeRedis()
    await finish_clients([client])


async def probe_cancelled_client_cleanup():
    cls = load_class("TikTokLive/client/client.py", "TikTokLiveClient",
                     dict(AsyncIOEventEmitter=object, asyncio=asyncio, inspect=__import__("inspect")))
    client = object.__new__(cls)
    closed = []

    async def ws_disconnect():
        pass

    async def web_close():
        closed.append(True)

    client._ws = NS(disconnect=ws_disconnect)
    client._web = NS(close=web_close, fetch_video_data=NS(is_recording=False))
    client._logger = LOGGER
    client._event_loop_task = asyncio.get_running_loop().create_future()
    client._event_loop_task.cancel()
    cancelled = False
    try:
        await client.disconnect(close_client=True)
    except asyncio.CancelledError:
        cancelled = True
    RESULTS["cancelled_client_cleanup"] = {
        "cancelled_error_escapes_disconnect": cancelled,
        "http_close_reached": bool(closed),
    }
    assert cancelled and not closed


async def probe_signer_close():
    cls = load_class("TikTokLive/client/web/web_base.py", "TikTokHTTPClient", {})
    client, closed = object.__new__(cls), []

    async def main_close():
        closed.append("main")

    async def signer_close():
        closed.append("signer")

    client._httpx = NS(aclose=main_close)
    client._curl_cffi = None
    client._tiktok_signer = NS(client=NS(aclose=signer_close))
    await client.close()
    RESULTS["http_resources"] = {"clients_closed": closed, "signer_close_reached": "signer" in closed}
    assert closed == ["main"]


def probe_offline_classification():
    class Offline(Exception):
        pass

    class FailedParse(Exception):
        pass

    route = load_class("TikTokLive/client/web/routes/fetch_room_id_live_html.py", "FetchRoomIdLiveHTMLRoute",
                       dict(ClientRoute=object, re=re, json=json, JSONDecodeError=json.JSONDecodeError,
                            UserOfflineError=Offline, FailedParseRoomIdError=FailedParse))
    html = '<script id="SIGI_STATE" type="application/json">' + json.dumps({
        "LiveRoom": {"liveRoomUserInfo": {"user": {"status": 4, "roomId": "7"}}}
    }) + '</script>'
    try:
        route.parse_room_id(html)
    except Exception as exc:
        RESULTS["offline_classification"] = {"expected": "Offline", "actual": type(exc).__name__}
        assert isinstance(exc, FailedParse)


async def main():
    await probe_duplicate_collectors()
    await probe_stale_state()
    await probe_broadcast()
    await probe_redis_callback()
    await probe_cancelled_client_cleanup()
    await probe_signer_close()
    probe_offline_classification()
    print(json.dumps({"mode": "offline_source_probes", "results": RESULTS}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
