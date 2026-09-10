import asyncio
import math
from asyncio import Task
from contextlib import aclosing
from typing import AsyncIterator, Optional, Type, Union

import httpx
from betterproto import Message
from websockets.legacy.client import WebSocketClientProtocol

from TikTokLive.client.logger import TikTokLiveLogHandler
from TikTokLive.client.web.web_settings import WebDefaults
from TikTokLive.client.ws.ws_connect import (
    WebcastConnect,
    WebcastProxy,
    WebcastProxyConnect,
)
from TikTokLive.proto import ProtoMessageFetchResult
from TikTokLive.proto.custom_extras import (
    HeartbeatMessage,
    WebcastImEnterRoomMessage,
    WebcastPushFrame,
)


class WebcastWSClient:
    """Websocket client responsible for connections to TikTok"""

    DEFAULT_PING_INTERVAL: float = 5.0

    def __init__(
            self,
            ws_kwargs: Optional[dict] = None,
            ws_proxy: Optional[WebcastProxy] = None
    ):
        """
        Initialize WebcastWSClient

        :param ws_kwargs: Overrides for the websocket connection

        """

        self._seq_id: int = 1
        self._ws_kwargs: dict = dict(ws_kwargs or {})
        self._send_timeout = float(self._ws_kwargs.pop("send_timeout", 10.0))
        if not math.isfinite(self._send_timeout) or self._send_timeout <= 0:
            raise ValueError("send_timeout must be a positive finite number")
        self._heartbeat_error: Optional[Exception] = None
        self._logger = TikTokLiveLogHandler.get_logger()
        self._ping_loop: Optional[Task] = None
        self._ws_proxy: Optional[WebcastProxy] = ws_proxy or self._ws_kwargs.pop("proxy", None)
        self._connect_generator_class: Union[Type[WebcastConnect], Type[WebcastProxyConnect]] = WebcastProxyConnect if self._ws_proxy else WebcastConnect
        self._connection_generator: Optional[WebcastConnect] = None

    @property
    def ws(self) -> Optional[WebSocketClientProtocol]:
        """
        Get the current WebSocketClientProtocol

        :return: WebSocketClientProtocol

        """

        # None because there's no generator
        if not self._connection_generator:
            return None

        # Optional[WebSocketClientProtocol]
        return self._connection_generator.ws

    @property
    def connected(self) -> bool:
        """
        Check if the WebSocket is open

        :return: WebSocket status

        """

        return bool(self.ws and self.ws.open)

    async def send(self, message: Union[bytes, Message]) -> None:
        """
        Send a message to the WebSocket

        :param message: Message to send to the WebSocket connection

        """

        # Can't send a message to a disconnected WebSocket
        if not self.connected:
            self._logger.warning("Attempted to send a message without an open WebSocket connection.")
            return

        # Log outbound data
        self._logger.debug("Sending frame to Webcast Server")

        # Send the data (+ Serialize the data if it's a protobuf message)
        await asyncio.wait_for(
            self.ws.send(message=bytes(message) if isinstance(message, Message) else message),
            timeout=self._send_timeout,
        )

    async def send_ack(
            self,
            webcast_response: ProtoMessageFetchResult,
            webcast_push_frame: WebcastPushFrame
    ) -> None:
        """
        Acknowledge the receipt of a ProtoMessageFetchResult message from TikTok, if necessary

        :param webcast_response: The ProtoMessageFetchResult to acknowledge
        :param webcast_push_frame: The WebcastPushFrame containing the ProtoMessageFetchResult
        :return: None

        """

        # Can't ack a dead websocket...
        if not self.connected:
            return

        # Send the ack
        await self.send(
            message=WebcastPushFrame(
                payload_type="ack",
                payload_encoding="pb",
                # ID of the WebcastPushMessage for the acknowledgement
                log_id=webcast_push_frame.log_id,
                # [Unknown] Hypothesized to be an acknowledgement of the ProtoMessageFetchResult (& its messages) within the WebcastPushMessage
                payload=(webcast_response.internal_ext or "-").encode()
            )
        )

    async def disconnect(self) -> None:
        """
        Request to stop the websocket connection & wait
        :return: None

        """

        if not self.connected:
            return

        await self.ws.close()

    def get_ws_cookie_string(self, cookies: httpx.Cookies) -> str:
        """
        Get the cookie string for the WebSocket connection.

        :param cookies: Cookies to pass to the WebSocket connection
        :return: Cookie string
        """

        cookie_values = []
        session_id: str | None = cookies.get("sessionid")

        # Exclude all session ID's if session_id exists and authentication is not required
        for key, value in cookies.items():
            cookie_values.append(f"{key}={value};")

        cookie_string = " ".join(cookie_values)

        self._logger.debug("Built WS cookies (authenticated=%s)", bool(session_id))

        return cookie_string

    async def connect(
            self,
            room_id: int,
            cookies: httpx.Cookies,
            user_agent: str,
            initial_webcast_response: ProtoMessageFetchResult,
            process_connect_events: bool = True,
            compress_ws_events: bool = True
    ) -> AsyncIterator[ProtoMessageFetchResult]:
        """
        Connect to the Webcast server & iterate over response messages.

        --- Message 1 ---

        The iterator exits normally when the connection is closed with close code
        1000 (OK) or 1001 (going away) or without a close code. It raises
        a :exc:`~websockets.exceptions.ConnectionClosedError` when the connection
        is closed with any other code.

        --- Message 2 ---

        DEVELOP SANITY NOTE:

        When ping_timeout is set (i.e. not None), the client waits for a pong for N seconds.
        TikTok DO NOT SEND pongs back. Unfortunately the websockets client after N seconds assumes the server is dead.
        It then throws the following infamous exception:

        websockets.exceptions.ConnectionClosedError: sent 1011 (unexpected error) keepalive ping timeout; no close frame received

        If you set ping_timeout to None, it doesn't wait for a pong. Perfect, since TikTok don't send them.

        --- Parameters --

        :param initial_webcast_response: The Initial ProtoMessageFetchResult from the sign server - NOT a PushFrame
        :param room_id: The room ID to connect to
        :param user_agent: The user agent to pass to the WebSocket connection
        :param cookies: The cookies to pass to the WebSocket connection
        :param process_connect_events: Whether to process the initial events sent in the first fetch
        :param compress_ws_events: Whether to ask TikTok to gzip the WebSocket events
        :return: Yields ProtoMessageFetchResultMessage, the messages within ProtoMessageFetchResult.messages

        """

        # Copy as to not affect the internal state
        ws_kwargs: dict = self._ws_kwargs.copy()

        if self._ws_proxy is not None:
            ws_kwargs["proxy_conn_timeout"] = ws_kwargs.get("proxy_conn_timeout", 10.0)
            ws_kwargs["proxy"] = self._ws_proxy

        # If we don't want to process these, remove them
        if not process_connect_events:
            initial_webcast_response.messages = []

        # Initialize the WebcastConnect class
        self._connection_generator: WebcastConnect = self._connect_generator_class(
            initial_webcast_response=initial_webcast_response,
            subprotocols=ws_kwargs.pop("subprotocols", ["echo-protocol"]),
            logger=self._logger,
            uri=ws_kwargs.pop('uri', None),  # Always *should* be none as we build this internally
            base_uri_append_str=(ws_kwargs.pop("base_uri_append_str", WebDefaults.ws_client_params_append_str)),

            # Base URI parameters
            base_uri_params={
                **WebDefaults.ws_client_params,
                **ws_kwargs.pop("base_uri_params", {}),
                "room_id": room_id,
                "compress": "gzip" if compress_ws_events else "",
            },

            # Extra headers
            extra_headers={
                # Must pass cookies to connect to the WebSocket
                "Cookie": self.get_ws_cookie_string(cookies=cookies),
                "User-Agent": user_agent,

                # Optional override for the headers
                **ws_kwargs.pop("extra_headers", {})
            },

            # Pass any extra  kwargs
            **{
                **ws_kwargs,
                "ping_timeout": None,
                "ping_interval": None,
            }
        )

        self._heartbeat_error = None
        try:
            # Explicitly close the inner generator even if the consumer is cancelled.
            async with aclosing(self._connection_generator.__aiter__()) as responses:
                async for webcast_push_frame, webcast_response in responses:
                    if webcast_response.is_first:
                        await self.enter_room(room_id=room_id)
                    if webcast_response.need_ack and webcast_push_frame is not None:
                        await self.send_ack(webcast_response, webcast_push_frame)
                    yield webcast_response
                    if not self.connected:
                        break
            if self._heartbeat_error is not None:
                raise self._heartbeat_error
        except Exception:
            if self._heartbeat_error is not None:
                raise self._heartbeat_error
            raise
        finally:
            ping_task, self._ping_loop = self._ping_loop, None
            try:
                if ping_task is not None:
                    ping_task.cancel()
                    await asyncio.gather(ping_task, return_exceptions=True)
            finally:
                self._connection_generator = None

    def restart_ping_loop(self, room_id: int) -> None:
        """
        Restart the WebSocket ping loop

        """

        if self._ping_loop:
            self._ping_loop.cancel()

        self._seq_id = 1
        self._ping_loop = asyncio.create_task(self._ping_loop_fn(room_id))

    async def enter_room(self, room_id: int) -> None:
        """Activate continuous event delivery after the WebSocket handshake."""

        enter_room_message = WebcastImEnterRoomMessage(
            room_id=room_id,
            room_tag="",
            live_id=12,
            identity="audience",
            cursor="",
            account_type=0,
            enter_unique_id=0,
            filter_welcome_msg="0",
            is_anchor_continue_keep_msg=False,
        )

        await self.send(
            message=WebcastPushFrame(
                payload_type="im_enter_room",
                payload_encoding="pb",
                payload=bytes(enter_room_message),
            )
        )
        self.restart_ping_loop(room_id=room_id)

    async def _ping_loop_fn(self, room_id: int) -> None:
        """
        Send a ping every 10 seconds to keep the connection alive

        """

        try:
            if not self.connected:
                return
            ping_interval = self.DEFAULT_PING_INTERVAL
            if self._connection_generator is not None and self._connection_generator.ws_options:
                try:
                    proposed = float(self._connection_generator.ws_options.get("ping-interval", ping_interval))
                    if math.isfinite(proposed) and proposed > 0:
                        ping_interval = proposed
                except (TypeError, ValueError):
                    self._logger.warning("Invalid upstream heartbeat interval; using default")
            while self.connected:
                heartbeat = HeartbeatMessage(room_id=room_id, send_packet_seq_id=self._seq_id)
                self._seq_id += 1
                await self.send(WebcastPushFrame(
                    payload_encoding="pb", payload_type="hb", payload=bytes(heartbeat), headers={},
                ))
                await asyncio.sleep(ping_interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._heartbeat_error = exc
            self._logger.warning("Heartbeat failed; closing upstream connection", exc_info=True)
            # A failed/timed-out send already proves this transport is unusable.
            # Waiting for its graceful close handshake would delay reconnection
            # by several close_timeout intervals on a blackholed network.
            ws = self.ws
            if ws is not None:
                ws.transport.abort()
