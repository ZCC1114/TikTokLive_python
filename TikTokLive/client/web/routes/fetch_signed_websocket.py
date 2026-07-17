import asyncio
import json
import os
from http.cookies import SimpleCookie
from json import JSONDecodeError
from typing import Optional

import httpx
from httpx import Response

from TikTokLive.client.errors import SignAPIError, SignatureRateLimitError
from TikTokLive.client.web.web_base import ClientRoute
from TikTokLive.client.web.web_settings import WebDefaults, CLIENT_NAME
from TikTokLive.client.web.web_utils import check_authenticated_session
from TikTokLive.client.ws.ws_utils import extract_webcast_response_message
from TikTokLive.proto import ProtoMessageFetchResult
from TikTokLive.proto.custom_extras import WebcastPushFrame


def _get_float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class FetchSignedWebSocketRoute(ClientRoute):
    """
    Call the signature server to receive the TikTok websocket URL

    """

    async def __call__(
            self,
            room_id: Optional[int] = None,
            preferred_agent_ids: list[str] = None,
            session_id: Optional[str] = None,
            tt_target_idc: Optional[str] = None,
    ) -> ProtoMessageFetchResult:
        """
        Call the method to get the first ProtoMessageFetchResult (as bytes) to use to upgrade to WebSocket & perform the first ack

        :param room_id: Override the room ID to fetch the webcast for
        :param preferred_agent_ids: The preferred agent ID to use for the request
        :return: The ProtoMessageFetchResult forwarded from the sign server proxy, as raw bytes

        """

        signer_client: httpx.AsyncClient = self._web.signer.client

        sign_params: dict = {
            'client': CLIENT_NAME,
            'room_id': room_id or self._web.params.get('room_id', None),
            'user_agent': self._web.headers['User-Agent'],
            'platform': 'web',
            'client_enter': 'true',
        }

        if preferred_agent_ids is not None:
            sign_params['preferred_agent_ids'] = ",".join(preferred_agent_ids)

        # The session ID we want to add to the request
        session_id: str = session_id or self._web.cookies.get('sessionid')
        tt_target_idc: str = tt_target_idc or self._web.cookies.get('tt-target-idc')

        if check_authenticated_session(session_id, tt_target_idc, session_required=False):
            sign_params['session_id'] = session_id
            sign_params['tt_target_idc'] = tt_target_idc
            self._logger.warning("Sending session ID to sign server for WebSocket connection. This is a risky operation.")

        timeout_seconds: float = _get_float_env("SIGN_API_TIMEOUT_SECONDS", 20.0)
        retries: int = max(_get_int_env("SIGN_API_RETRIES", 2), 0)
        retry_backoff_seconds: float = max(_get_float_env("SIGN_API_RETRY_BACKOFF_SECONDS", 1.0), 0.0)
        retryable_status_codes = {500, 502, 503, 504}
        response: Optional[httpx.Response] = None

        for attempt in range(retries + 1):
            try:
                response = await signer_client.get(
                    url=WebDefaults.tiktok_sign_url + "/webcast/fetch",
                    params=sign_params,
                    timeout=timeout_seconds,
                )

                if response.status_code in retryable_status_codes and attempt < retries:
                    wait_seconds = retry_backoff_seconds * (attempt + 1)
                    response_preview = response.text.replace("\n", " ")[:200]
                    self._logger.warning(
                        "Sign API returned HTTP %s (attempt %s/%s): %s; retrying in %.1fs",
                        response.status_code,
                        attempt + 1,
                        retries + 1,
                        response_preview,
                        wait_seconds,
                    )
                    if wait_seconds:
                        await asyncio.sleep(wait_seconds)
                    continue

                break
            except httpx.ReadTimeout:
                if attempt >= retries:
                    raise

                wait_seconds = retry_backoff_seconds * (attempt + 1)
                self._logger.warning(
                    "Sign API ReadTimeout for room_id=%s, retrying in %.1fs (%s/%s)",
                    sign_params.get("room_id"),
                    wait_seconds,
                    attempt + 1,
                    retries,
                )
                if wait_seconds:
                    await asyncio.sleep(wait_seconds)
            except httpx.ConnectError as ex:
                raise SignAPIError(
                    SignAPIError.ErrorReason.CONNECT_ERROR,
                    "Failed to connect to the sign server due to an httpx.ConnectError!",
                    response=None
                ) from ex

        if response is None:
            raise SignAPIError(
                SignAPIError.ErrorReason.EMPTY_PAYLOAD,
                "Sign API did not return a response.",
                response=None
            )

        self._logger.debug(
            f"Attempted to fetch WebSocket information fetch from the Sign Server API! <-> "
            f"Status: {response.status_code} - "
            f"Agent ID: \"{response.headers.get('X-Agent-Id', 'N/A')}\" - "
            f"Log ID: {response.headers.get('X-Log-Id')} - "
            f"Log Code: {response.headers.get('X-Log-Code')} "
            f"<->"
        )

        data: bytes = await response.aread()

        if response.status_code == 429:
            data_json = response.json()
            server_message: Optional[str] = None if os.environ.get('SIGN_SERVER_MESSAGE_DISABLED') else data_json.get("message")
            limit_label: str = f"({data_json['limit_label']}) " if data_json.get("limit_label") else ""

            raise SignatureRateLimitError(
                server_message,
                (
                    f"{limit_label}Too many connections started, try again in %s seconds."
                ),
                response=response
            )

        elif not data:
            raise SignAPIError(
                SignAPIError.ErrorReason.EMPTY_PAYLOAD,
                f"Sign API returned an empty request. Are you being detected by TikTok?",
                response=response
            )

        elif not response.status_code == 200:

            try:
                payload: str = json.dumps(response.json(), indent=2)
            except JSONDecodeError:
                payload: str = f'"{response.text}"'

            raise SignAPIError(
                SignAPIError.ErrorReason.SIGN_NOT_200,
                f"Failed request to Sign API with status code {response.status_code} and the following payload:\n{payload}",
                response=response
            )

        # Update web params & cookies
        self._update_client_cookies(response)
        response_data: bytes = response.read()

        # Package it in a push frame & parse it to maintain parity with the WebcastWebSocket
        return extract_webcast_response_message(
            logger=self._logger,
            push_frame=WebcastPushFrame(
                log_id=-1,
                payload=response_data,
                payload_type="msg"
            ),
        )

    def _update_client_cookies(self, response: Response) -> None:
        """
        Update the cookies in the cookie jar from the sign server response

        :param response: The `httpx.Response` to parse for cookies
        :return: None

        """

        jar: SimpleCookie = SimpleCookie()
        cookies_header: Optional[str] = response.headers.get("X-Set-TT-Cookie")

        if not cookies_header:
            raise SignAPIError(
                SignAPIError.ErrorReason.EMPTY_COOKIES,
                "Sign server did not return cookies!",
                response=response
            )

        jar.load(cookies_header)
        for cookie, morsel in jar.items():

            # If it has the cookie, delete the cookie first
            if self._web.cookies.get(cookie):
                self._web.cookies.delete(cookie)

            self._web.cookies.set(cookie, morsel.value, ".tiktok.com")
