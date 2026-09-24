import logging
import socket
from typing import Any, AsyncIterator, Dict, Optional, Tuple, Type, Union

import httpx
from python_socks import ProxyType, parse_proxy_url
from websockets import InvalidStatusCode
from websockets.legacy.client import Connect, WebSocketClientProtocol
from websockets_proxy import websockets_proxy
from websockets_proxy.websockets_proxy import ProxyConnect

from TikTokLive.client.errors import WebcastBlocked200Error
from TikTokLive.client.ws.ws_utils import (
    build_webcast_uri,
    extract_webcast_response_message,
    extract_websocket_options,
)
from TikTokLive.proto import ProtoMessageFetchResult
from TikTokLive.proto.custom_extras import WebcastPushFrame

"""Type hint for a WebcastProxy, which can be either an HTTPX Proxy or a Websockets Proxy"""
WebcastProxy: Type = Union[httpx.Proxy, websockets_proxy.Proxy]

"""
Type hint for a WebcastIterator, which yields a tuple of WebcastPushFrame and ProtoMessageFetchResult.
WebcastPushFrame is Optional because the first yielded item is from the initial response
which is from /im/fetch (from the sign server), so it is not encapsulated by a WebcastPushFrame.
"""
WebcastIterator: Type = AsyncIterator[Tuple[Optional[WebcastPushFrame], ProtoMessageFetchResult]]


class WebcastConnect(Connect):

    def __init__(
            self,
            initial_webcast_response: ProtoMessageFetchResult,
            logger: logging.Logger,
            base_uri_params: Dict[str, Any],
            base_uri_append_str: str,
            uri: Optional[str] = None,
            tcp_keepalive: bool = False,
            tcp_keepidle: int = 30,
            tcp_keepintvl: int = 10,
            tcp_keepcnt: int = 3,
            tcp_user_timeout: int = 60000,
            **kwargs
    ):

        keepalive_options = {
            "tcp_keepidle": tcp_keepidle,
            "tcp_keepintvl": tcp_keepintvl,
            "tcp_keepcnt": tcp_keepcnt,
            "tcp_user_timeout": tcp_user_timeout,
        }
        for name, value in keepalive_options.items():
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._keepalive_options = keepalive_options

        # If uri is provided (it should normally never be), bypass the construction
        if uri is None:
            uri: str = build_webcast_uri(
                initial_webcast_response=initial_webcast_response,
                base_uri_params=base_uri_params,
                base_uri_append_str=base_uri_append_str
            )

        super().__init__(uri, logger=logger, **kwargs)
        self.logger = self._logger = logger
        self.logger.debug("Built Webcast connection URI")
        self._ws: Optional[WebSocketClientProtocol] = None
        self._ws_options: Optional[dict[str, str]] = None
        self._initial_response: ProtoMessageFetchResult = initial_webcast_response
        self._tcp_keepalive = tcp_keepalive

    def _configure_keepalive(self, protocol: WebSocketClientProtocol) -> None:
        """Detect dead TCP peers without requiring TikTok WebSocket pongs."""
        sock = protocol.transport.get_extra_info("socket")
        if sock is None:
            return
        options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        idle = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
        if idle is not None:
            options.append((socket.IPPROTO_TCP, idle, self._keepalive_options["tcp_keepidle"]))
        for name, value in (
            ("TCP_KEEPINTVL", self._keepalive_options["tcp_keepintvl"]),
            ("TCP_KEEPCNT", self._keepalive_options["tcp_keepcnt"]),
            ("TCP_USER_TIMEOUT", self._keepalive_options["tcp_user_timeout"]),
        ):
            option = getattr(socket, name, None)
            if option is not None:
                options.append((socket.IPPROTO_TCP, option, value))
        for level, option, value in options:
            try:
                sock.setsockopt(level, option, value)
            except OSError:
                self._logger.debug("TCP keepalive option unavailable: %s", option)

    @property
    def ws(self) -> Optional[WebSocketClientProtocol]:
        """Get the current WebSocketClientProtocol"""

        return self._ws

    @property
    def ws_options(self) -> Optional[dict[str, str]]:
        """Get the WebSocket options as returned via the Handshake-Options header"""

        return self._ws_options

    async def __aiter__(self) -> WebcastIterator:
        """
        Note as of Jul 6, 2025

        - This is a custom implementation over the default iterator
        - It disables retry mechanisms and **disallows** reconnects, since signed URLs expire after 30 seconds
        - Also, the default mechanism by the websockets library ignores the '200' error code and retries, even though this is a 'detected by TikTok' error & thus
          retrying is useless.

        tl;dr This goes from an iterator of WebSockets -> an iterator of Events

        """

        try:

            # "async with" yields a WebsocketClientProtocol
            # The connection happens in the "async with", so if you enter the loop, that means it connected to the WebSocket
            async with self as protocol:
                self._ws = protocol
                if self._tcp_keepalive:
                    self._configure_keepalive(protocol)
                self._ws_options = extract_websocket_options(self._ws.response_headers)

                # Yield the first ProtoMessageFetchResult
                self._initial_response.is_first = True
                yield None, self._initial_response

                # "async for" yields "WebcastPushFrame" payloads as unparsed bytes
                async for payload_bytes in protocol:

                    # Extract push frame
                    webcast_push_frame: WebcastPushFrame = WebcastPushFrame().parse(payload_bytes)

                    # Only deal with messages
                    if webcast_push_frame.payload_type != "msg":
                        self._logger.debug("Received non-message frame: %s", webcast_push_frame.payload_type)
                        continue

                    # If it is of type msg, we can extract the ProtoMessageFetchResult item within
                    webcast_response: ProtoMessageFetchResult = extract_webcast_response_message(webcast_push_frame, logger=self._logger)
                    yield webcast_push_frame, webcast_response

        except InvalidStatusCode as ex:
            if ex.status_code == 200:
                # Note from Isaac post-insanity...
                # IF the WebSockets are >>SIGNED<< WITH A SESSION ID
                # and you DO NOT pass a sessionid cookie in the header, it will reject for "illegal secret key"
                raise WebcastBlocked200Error(
                    f"WebSocket rejected by TikTok due to \"{ex.headers.get('Handshake-Msg', 'an unknown reason')}\"."
                ) from ex
            raise

        finally:
            self._ws = None
            self._ws_options = None


class WebcastProxyConnect(WebcastConnect, ProxyConnect):
    """
    Add Proxy support to the WebcastConnect class

    """

    def __init__(
            self,
            proxy: Optional[WebcastProxy],
            **kwargs
    ):
        super().__init__(
            proxy=self._convert_proxy(proxy) if isinstance(proxy, httpx.Proxy) else proxy,
            **kwargs
        )

    @classmethod
    def _convert_proxy(cls, proxy: httpx.Proxy) -> websockets_proxy.Proxy:
        """Convert an HTTPX proxy to a websockets_proxy Proxy"""
        parsed: Tuple[ProxyType, str, int, Optional[str], Optional[str]] = parse_proxy_url(str(proxy.url))
        parsed: list = list(parsed)

        # Add auth back
        if proxy.auth:
            parsed[3], parsed[4] = proxy.auth

        return websockets_proxy.Proxy(*parsed)
