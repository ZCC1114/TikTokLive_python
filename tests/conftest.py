import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

from live_service.config import Settings
from TikTokLive.events import CommentEvent, ConnectEvent
from TikTokLive.proto import CommonMessageData, User


def comment(message_id=1, content="你好 🌷"):
    return CommentEvent(
        base_message=CommonMessageData(message_id=message_id, room_id=7300000000000000001),
        user_info=User(id=7500000000000000001, username="viewer", nick_name="观众"),
        content=content,
    )


class Socket:
    def __init__(self, blocked=False):
        self.messages = []
        self.accepted = False
        self.closed = False
        self.close_code = None
        self.blocked = blocked
        self.gate = asyncio.Event()
        self.writers = 0
        self.max_writers = 0

    async def accept(self):
        self.accepted = True

    async def send_text(self, text):
        self.writers += 1
        self.max_writers = max(self.max_writers, self.writers)
        try:
            if self.blocked and text.startswith("{"):
                await self.gate.wait()
            await asyncio.sleep(0)
            self.messages.append(text)
        finally:
            self.writers -= 1

    async def close(self, code=1000):
        self.closed = True
        self.close_code = code


class FakeClient:
    def __init__(self, key, error=None, gate=None):
        self.key = key
        self.error = error
        self.gate = gate
        self.event_sink = None
        self.closed = False
        self.runtime = asyncio.get_running_loop().create_future()

    async def start(self):
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        await self.event_sink(ConnectEvent(unique_id=self.key, room_id=7300000000000000001))
        return self.runtime

    async def disconnect(self, close_client=False):
        self.closed = True
        if not self.runtime.done():
            self.runtime.set_result(None)


class Factory:
    def __init__(self, errors=(), gate=None):
        self.clients = []
        self.errors = list(errors)
        self.gate = gate

    def __call__(self, key):
        client = FakeClient(key, self.errors.pop(0) if self.errors else None, self.gate)
        self.clients.append(client)
        return client


class Enricher:
    def __init__(self, gate=None):
        self.closed = False
        self.gate = gate

    async def enrich(self, message):
        if self.gate:
            await self.gate.wait()
        message.update(orderNumber="", blackLevel="0", createdUsers=[], metadataAvailable=True)

    async def aclose(self):
        self.closed = True


async def eventually(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
def settings():
    return replace(
        Settings(),
        idle_grace=0.01,
        retry_initial=0.01,
        retry_max=0.02,
        cleanup_timeout=0.2,
        drain_timeout=0.1,
        send_timeout=0.1,
    )


@pytest_asyncio.fixture(autouse=True)
async def no_service_task_leaks():
    yield
    await asyncio.sleep(0)
    leaked = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and t.get_name().startswith("live-") and not t.done()
    ]
    assert not leaked, f"Leaked service tasks: {leaked}"
