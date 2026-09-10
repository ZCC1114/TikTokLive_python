import asyncio
import inspect
import logging
import time
import traceback
from asyncio import AbstractEventLoop, CancelledError, Task
from contextlib import aclosing
from logging import Logger
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Type,
    Union,
)

import httpx
from pyee.asyncio import AsyncIOEventEmitter
from pyee.base import Handler

from TikTokLive.client.diagnostics import CommentDiagnostics
from TikTokLive.client.errors import (
    AlreadyConnectedError,
    UserOfflineError,
)
from TikTokLive.client.logger import LogLevel, TikTokLiveLogHandler
from TikTokLive.client.web.routes.fetch_user_unique_id import FailedResolveUserId
from TikTokLive.client.web.web_client import TikTokWebClient
from TikTokLive.client.web.web_settings import WebDefaults
from TikTokLive.client.ws.ws_client import WebcastWSClient
from TikTokLive.client.ws.ws_connect import WebcastProxy
from TikTokLive.events import CommentEvent, ControlEvent, Event, EventHandler
from TikTokLive.events.custom_events import (
    ConnectEvent,
    CustomEvent,
    DisconnectEvent,
    FollowEvent,
    LiveEndEvent,
    LivePauseEvent,
    LiveUnpauseEvent,
    ShareEvent,
    UnknownEvent,
    WebsocketResponseEvent,
)
from TikTokLive.events.proto_events import EVENT_MAPPINGS, ProtoEvent
from TikTokLive.proto import (
    ProtoMessageFetchResult,
    ProtoMessageFetchResultBaseProtoMessage,
)
from TikTokLive.proto.custom_proto import ControlAction


class TikTokLiveClient(AsyncIOEventEmitter):
    """
    A client to connect to & read from TikTok LIVE streams

    """

    def __init__(
            self,
            # User to connect to
            unique_id: str | int,

            # Proxies
            web_proxy: Optional[httpx.Proxy] = None,
            ws_proxy: Optional[WebcastProxy] = None,

            # Client kwargs
            web_kwargs: Optional[dict] = None,
            ws_kwargs: Optional[dict] = None,

            is_userid: Optional[bool] = False
    ):
        """
        Instantiate the TikTokLiveClient client

        :param unique_id: The username of the creator to connect to
        :param web_proxy: An optional proxy used for HTTP requests
        :param ws_proxy: An optional proxy used for the WebSocket connection
        :param web_kwargs: Optional arguments used by the HTTP client
        :param ws_kwargs: Optional arguments used by the WebSocket client
        :param is_userid: Optional argument to resolve userid to unique_id

        """

        super().__init__()
        self._starting = False
        self._disconnect_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self.connect_timings: dict[str, float] = {}
        self.connection_phase: str = "idle"
        self._connection_started_at: float | None = None
        self._handshake_started_at: float | None = None

        self._ws: WebcastWSClient = WebcastWSClient(
            ws_kwargs=ws_kwargs or {},
            ws_proxy=ws_proxy
        )

        web_kwargs = dict(web_kwargs or {})
        self._web: TikTokWebClient = TikTokWebClient(
            web_proxy=web_proxy or web_kwargs.pop("web_proxy", None),
            **web_kwargs
        )

        self._web.params['referer'] = f"https://www.tiktok.com/@{unique_id}/live"
        self._web.params['root_referer'] = f"https://www.tiktok.com/@{unique_id}/live"

        self._logger: Logger = TikTokLiveLogHandler.get_logger(
            level=LogLevel.ERROR
        )

        # Overridable properties
        self.ignore_broken_payload: bool = False
        # Optional awaitable sink for services that need bounded backpressure.
        # Normal pyee listeners retain their original behavior.
        self.event_sink = None
        # Services can opt out of materializing unused protobuf envelope copies.
        # Explicit raw-event listeners always retain their original events.
        self.process_raw_events: bool = True
        self.comment_diagnostics = CommentDiagnostics()

        # Properties
        self._is_userid: bool = is_userid
        self._unique_id: str = self.parse_unique_id(unique_id)
        self._room_id: Optional[int] = None
        self._room_info: Optional[Dict[str, Any]] = None
        self._gift_info: Optional[Dict[str, Any]] = None
        self._event_loop_task: Optional[Task] = None

    @classmethod
    def parse_unique_id(cls, unique_id: str) -> str:
        """
        Parse unique ID from a generic string

        :param unique_id: The unique_id to parse
        :return: The parsed unique_id

        """

        return str(unique_id) \
            .replace(WebDefaults.tiktok_app_url + "/", "") \
            .replace("/live", "") \
            .replace("@", "", 1) \
            .strip()

    async def start(
            self,
            *,
            process_connect_events: bool = True,
            compress_ws_events: bool = True,
            fetch_room_info: bool = False,
            fetch_gift_info: bool = False,
            fetch_live_check: bool = True,
            room_id: Optional[int] = None,
            preferred_agent_ids: Optional[list[str]] = None,
            wait_connected: bool = False,
            sign_api_timeout: float | None = None,
            sign_api_retries: int | None = None,
    ) -> Task:
        """
        Create a non-blocking connection to TikTok LIVE and return the task

        :param process_connect_events: Whether to process initial events sent on room join
        :param fetch_room_info: Whether to fetch room info on join
        :param fetch_gift_info: Whether to fetch gift info on join
        :param fetch_live_check: Whether to check if the user is live (you almost ALWAYS want this enabled)
        :param room_id: An override to the room ID to connect directly to the livestream and skip scraping the live.
                        Useful when trying to scale, as scraping the HTML can result in TikTok blocks.
        :param compress_ws_events: Whether to compress the WebSocket events using gzip compression (you should probably have this on)
        :param preferred_agent_ids: The preferred agent IDs to use when connecting to the WebSocket
        :param wait_connected: Wait for the WebSocket handshake and enter-room send before returning.
                               Cancellation then also closes the pending reader/transport.
        :param sign_api_timeout: Optional timeout for each signing request, in seconds.
        :param sign_api_retries: Optional signing retries; services with a retry supervisor can use zero.
        :return: Task containing the heartbeat of the client

        """

        if self._starting:
            raise AlreadyConnectedError("A connection attempt is already running!")
        self._starting = True
        try:
            if self._ws.connected or (self._event_loop_task is not None and not self._event_loop_task.done()):
                raise AlreadyConnectedError("You can only make one connection per client!")

            self.connect_timings = {}
            self._connected_event.clear()
            self._connection_started_at = time.monotonic()
            self._handshake_started_at = None
            self._unique_id = await self._connection_step("resolve_user", self._resolve_user_id(self._unique_id))

            # HTML resolution owns its API fallback and preserves error types.
            self._room_id = int(room_id or await self._connection_step(
                "resolve_room", self._web.fetch_room_id_from_html(self._unique_id)
            ))
            self.connect_timings.setdefault("resolve_room_seconds", 0.0)

            # Gram Room ID
            self._web.params["room_id"] = str(self._room_id) or None

            # <Optional> Fetch live status
            if fetch_live_check:
                if not await self._connection_step("live_check", self._web.fetch_is_live(room_id=self._room_id)):
                    raise UserOfflineError()
            else:
                self.connect_timings["live_check_seconds"] = 0.0

            # <Optional> Fetch room info
            if fetch_room_info:
                self._room_info = await self._connection_step("room_info", self._web.fetch_room_info())

            # <Optional> Fetch gift info
            if fetch_gift_info:
                self._gift_info = await self._connection_step("gift_info", self._web.fetch_gift_list())

            # <Required> Fetch the first response
            sign_kwargs = {"preferred_agent_ids": preferred_agent_ids}
            if sign_api_timeout is not None:
                sign_kwargs["timeout_seconds"] = sign_api_timeout
            if sign_api_retries is not None:
                sign_kwargs["retries"] = sign_api_retries
            initial_webcast_response: ProtoMessageFetchResult = await self._connection_step(
                "sign", self._web.fetch_signed_websocket(**sign_kwargs)
            )

            # Start the websocket connection & return it
            self.connection_phase = "handshake"
            self._handshake_started_at = time.monotonic()
            self._event_loop_task = self._asyncio_loop.create_task(
                self._ws_client_loop(
                    initial_webcast_response=initial_webcast_response,
                    process_connect_events=process_connect_events,
                    compress_ws_events=compress_ws_events
                )
            )

            if wait_connected:
                await self._wait_for_connection()
            return self._event_loop_task
        finally:
            self._starting = False
            if self._connection_started_at is not None and not self._connected_event.is_set():
                self.connect_timings["total_seconds"] = time.monotonic() - self._connection_started_at

    async def _connection_step(self, name: str, operation):
        """Record phase durations without collecting signed URLs or credentials."""
        self.connection_phase = name
        started_at = time.monotonic()
        try:
            return await operation
        finally:
            self.connect_timings[f"{name}_seconds"] = time.monotonic() - started_at

    async def _wait_for_connection(self) -> None:
        reader = self._event_loop_task
        ready = asyncio.create_task(self._connected_event.wait())
        try:
            done, _ = await asyncio.wait({reader, ready}, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                await reader
                raise ConnectionError("WebSocket closed before the connection became ready")
        except BaseException:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)


    async def connect(
            self,
            callback: Optional[
                Union[
                    Callable[[None], None],
                    Callable[[None], Coroutine[None, None, None]],
                    Coroutine[None, None, None],
                ]
            ] = None,
            **kwargs
    ) -> Task:
        """
        Start a future-blocking connection to TikTokLive

        :param callback: A callback function to run when connected
        :param kwargs: Kwargs to pass to start
        :return: The task, once it's finished

        """

        task: Task = await self.start(**kwargs)

        try:
            if inspect.iscoroutinefunction(callback):
                self._asyncio_loop.create_task(callback())
            elif inspect.isawaitable(callback):
                self._asyncio_loop.create_task(callback)
            elif inspect.isfunction(callback):
                callback()
            await task
        except CancelledError:
            self._logger.debug("The client has been manually stopped with 'client.stop()'.")

        return task

    def run(self, **kwargs) -> Task:
        """
        Start a thread-blocking connection to TikTokLive

        :param kwargs: Kwargs to pass to start
        :return: The task, once it's finished

        """

        return self._asyncio_loop.run_until_complete(self.connect(**kwargs))

    async def disconnect(self, close_client: bool = False) -> None:
        """
        Disconnect the client from the websocket.

        :param close_client: Whether to also close the HTTP client if you don't intend to reuse it
        :return: None

        """

        try:
            async with self._disconnect_lock:
                try:
                    await self._ws.disconnect()
                    task = self._event_loop_task
                    if task is not None and task is not asyncio.current_task():
                        try:
                            await asyncio.shield(task)
                        except CancelledError:
                            # A previously cancelled reader is an expected shutdown result.
                            # Cancellation of this caller must still propagate after cleanup.
                            if not task.cancelled():
                                raise
                        except Exception:
                            self._logger.debug("Reader failed during disconnect", exc_info=True)
                finally:
                    self._event_loop_task = None
                    try:
                        if self._web.fetch_video_data.is_recording:
                            self._web.fetch_video_data.stop()
                    finally:
                        self._room_id = None
                        self._room_info = None
                        self._gift_info = None
        finally:
            # A concurrent stream-end disconnect can already hold the lock.
            # Even cancellation while waiting for it must release owned pools.
            if close_client:
                await self.close()


    async def close(self) -> None:
        """
        Discards the async sessions if you don't intend to use the client again

        :return: None

        """

        await self._web.close()

    def on(self, event: Type[Event], f: Optional[EventHandler] = None) -> Union[Handler, Callable[[Handler], Handler]]:
        """
        Decorator that can be used to register a Python function as an event listener

        :param event: The event to listen to
        :param f: The function to handle the event
        :return: The wrapped function as a generated `pyee.Handler` object

        """

        return super(TikTokLiveClient, self).on(event.get_type(), f)

    def add_listener(self, event: Type[Event], f: EventHandler) -> Handler:
        """
        Method that can be used to register a Python function as an event listener

        :param event: The event to listen to
        :param f: The function to handle the event
        :return: The generated `pyee.Handler` object

        """
        if isinstance(event, str):
            return super().add_listener(event=event, f=f)

        return super().add_listener(event=event.get_type(), f=f)

    def has_listener(self, event: Type[Event]) -> bool:
        """
        Check whether the client is listening to a given event

        :param event: The event to check listening for
        :return: Whether it is being listened to

        """

        return event.__name__ in self._events

    async def _ws_client_loop(
            self,
            initial_webcast_response: ProtoMessageFetchResult,
            process_connect_events: bool,
            compress_ws_events: bool
    ) -> None:
        """
        Run the websocket loop to handle incoming WS events

        :param initial_webcast_response: The ProtoMessageFetchResult (as bytes) retrieved from the sign server with connection info
        :param process_connect_events: Whether to process initial events sent on room join
        :param compress_ws_events: Whether to compress the WebSocket events using gzip compression
        :return: None

        """

        responses = self._ws.connect(
            initial_webcast_response=initial_webcast_response,
            process_connect_events=process_connect_events,
            compress_ws_events=compress_ws_events,
            cookies=self._web.cookies,
            room_id=self._room_id,
            user_agent=self._web.headers['User-Agent'],
        )
        try:
            async with aclosing(responses):
                async for webcast_response in responses:
                    if webcast_response.is_first:
                        now = time.monotonic()
                        if self._handshake_started_at is not None:
                            self.connect_timings["handshake_seconds"] = now - self._handshake_started_at
                        if self._connection_started_at is not None:
                            self.connect_timings["total_seconds"] = now - self._connection_started_at
                        self.connection_phase = "connected"
                        self._connected_event.set()
                    async for event in self._parse_webcast_response(webcast_response):
                        if self._logger.isEnabledFor(logging.DEBUG):
                            self._logger.debug("Received Event '%s' [%s bytes]", event.type, event.size)
                        if self.event_sink is not None:
                            await self.event_sink(event)
                        self.emit(event.type, event)
        finally:
            if not self._connected_event.is_set() and self._handshake_started_at is not None:
                self.connect_timings["handshake_seconds"] = time.monotonic() - self._handshake_started_at
            ev: DisconnectEvent = DisconnectEvent()
            self.emit(ev.type, ev)


    async def _parse_webcast_response(self, webcast_response: ProtoMessageFetchResult) -> AsyncIterator[Event]:
        """
        Parse incoming webcast responses into events that can be emitted

        :param webcast_response: The ProtoMessageFetchResult protobuf message
        :return: A list of events that can be gleamed from this event

        """

        # The first event means we connected
        if webcast_response.is_first:
            yield ConnectEvent(unique_id=self._unique_id, room_id=self._room_id)

        # Yield events
        for message in webcast_response.messages:
            for event in await self._parse_webcast_response_message(webcast_response_message=message):
                if event is not None:
                    if type(event) is CommentEvent:
                        self.comment_diagnostics.observe(
                            event.base_message.room_id,
                            event.base_message.message_id,
                            initial=webcast_response.is_first,
                            payload=message.payload,
                        )
                    yield event

    async def _parse_webcast_response_message(
            self,
            webcast_response_message: Optional[ProtoMessageFetchResultBaseProtoMessage]
    ) -> List[Event]:
        """
        Parse incoming webcast responses into events that can be emitted

        :param webcast_response_message: The ProtoMessageFetchResultMessage protobuf message
        :return: A list of events that can be gleamed from this event

        """

        # Invalid response handler
        if webcast_response_message is None:
            self._logger.warning("Received a null ProtoMessageFetchResultMessage from the Webcast server.")
            return []

        # Get the proto mapping for proto-events
        event_type: Optional[Type[ProtoEvent]] = EVENT_MAPPINGS.get(webcast_response_message.method)
        response_event = (
            WebsocketResponseEvent().from_dict(webcast_response_message.to_dict())
            if self.process_raw_events or self.has_listener(WebsocketResponseEvent) else None
        )

        # If the event is not tracked, return
        if event_type is None:
            events = [UnknownEvent().from_dict(webcast_response_message.to_dict())]
            return [response_event, *events] if response_event is not None else events

        # Get the underlying events
        try:
            proto_event: ProtoEvent = event_type().parse(webcast_response_message.payload)
        except Exception:
            if not self.ignore_broken_payload:
                self._logger.error(
                    traceback.format_exc() + "\nBroken Payload:\n" + str(webcast_response_message.payload))
            return [response_event] if response_event is not None else []

        parsed_events: List[Event] = [response_event, proto_event] if response_event is not None else [proto_event]
        custom_event: Optional[Event] = await self.handle_custom_event(webcast_response_message, proto_event)

        # Add the custom event IF not null
        return [custom_event, *parsed_events] if custom_event else parsed_events

    async def is_live(self, unique_id: Optional[str | int] = None) -> bool:
        """
        Check if the client is currently live on TikTok

        :param unique_id: Optionally override the user to check
        :return: Whether they are live on TikTok

        """

        self._unique_id = unique_id = await self._resolve_user_id(unique_id or self.unique_id)

        return await self._web.fetch_is_live(unique_id=unique_id or self.unique_id)

    async def handle_custom_event(self, response: ProtoMessageFetchResultBaseProtoMessage, event: ProtoEvent) -> \
            Optional[CustomEvent]:
        """
        Extract CustomEvent events from existing ProtoEvent events

        :param response: The ProtoMessageFetchResultMessage to parse for the custom event
        :param event: The ProtoEvent to parse for the custom event
        :return: The event, if one exists

        """

        # LiveEndEvent, LivePauseEvent, LiveUnpauseEvent
        if isinstance(event, ControlEvent):
            if event.action in {
                ControlAction.CONTROL_ACTION_STREAM_ENDED,
                ControlAction.CONTROL_ACTION_STREAM_SUSPENDED
            }:
                # If the stream is over, disconnect the client. Can't await due to circular dependency.
                self._asyncio_loop.create_task(self.disconnect())
                return LiveEndEvent().parse(response.payload)
            elif event.action == ControlAction.CONTROL_ACTION_STREAM_PAUSED:
                return LivePauseEvent().parse(response.payload)
            elif event.action == ControlAction.CONTROL_ACTION_STREAM_UNPAUSED:
                return LiveUnpauseEvent().parse(response.payload)
            return None

        # FollowEvent
        if "follow" in event.base_message.display_text.key:
            return FollowEvent().parse(response.payload)

        # ShareEvent
        if "share" in event.base_message.display_text.key:
            return ShareEvent().parse(response.payload)

        # Not a custom event
        return None

    async def _resolve_user_id(self, unique_id: str | int) -> str:
        """Resolve a unique_id and return the resolved value"""
        parsed_id = self.parse_unique_id(unique_id)
        if parsed_id.isdigit() and self._is_userid:
            resolved_id = await self._web.fetch_user_unique_id(int(parsed_id))
            if not resolved_id:
                raise FailedResolveUserId(f"Resolved ID is invalid: {resolved_id}")
            return resolved_id
        return parsed_id

    async def send_room_chat(
            self,
            content: str
    ) -> Any:
        """
        Send a chat message to the room

        :param content: The content of the message
        :return: The response from TikTok

        """

        return await self._web.send_room_chat(content=content, room_id=self._room_id)

    @property
    def unique_id(self) -> str:
        """
        The cleaned unique-id parameter passed to the client

        """

        return self._unique_id

    @property
    def room_id(self) -> Optional[int]:
        """
        The room ID the user is currently connected to

        :return: Room ID or None

        """

        return self._room_id

    @property
    def web(self) -> TikTokWebClient:
        """
        The HTTP client that this client uses for requests

        :return: A copy of the TikTokWebClient

        """

        return self._web

    @property
    def _asyncio_loop(self) -> AbstractEventLoop:
        """
        Property to return the existing or generate a new asyncio event loop

        :return: An asyncio event loop

        """

        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.new_event_loop()

    @property
    def connected(self) -> bool:
        """
        Whether the WebSocket client is currently connected to TikTok

        :return: Connection status

        """

        return self._ws.connected

    @property
    def logger(self) -> logging.Logger:
        """
        The internal logger used by TikTokLive

        :return: An instance of a `logging.Logger`

        """

        return self._logger

    @property
    def gift_info(self) -> Optional[dict]:
        """
        Information about the stream's gifts *if* fetch_gift_info=True when starting the client e.g. with `client.run`)

        :return: The stream gift info

        """

        return self._gift_info

    @property
    def room_info(self) -> Optional[dict]:
        """
        Information about the room *if* fetch_room_info=True when starting the client (e.g. with `client.run`)

        :return: Dictionary of room info

        """

        return self._room_info
