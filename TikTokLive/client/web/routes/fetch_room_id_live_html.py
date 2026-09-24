import json
import re
from json import JSONDecodeError
from typing import Optional

from httpx import HTTPError, Response

from TikTokLive.client.errors import (
    TikTokLiveError,
    UserOfflineError,
)
from TikTokLive.client.web.web_base import ClientRoute
from TikTokLive.client.web.web_settings import WebDefaults


class FailedParseRoomIdError(TikTokLiveError):
    """
    Thrown when the Room ID cannot be parsed

    """


class FetchRoomIdLiveHTMLRoute(ClientRoute):
    """
    Route to retrieve the room ID for a user

    """

    SIGI_PATTERN: re.Pattern = re.compile(r"""<script id="SIGI_STATE" type="application/json">(.*?)</script>""")

    UNIVERSAL_PATTERN: re.Pattern = re.compile(r"""<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>""")

    async def __call__(self, unique_id: str) -> str:
        """
        Fetch the Room ID for a given unique_id from the page HTML

        :param unique_id: The user's username
        :return: The room ID string

        """

        # Own the fallback here, so an API failure isn't fetched a second time
        # by TikTokLiveClient.start(). Transport errors retain their type.
        try:
            response: Response = await self._web.get(
                url=WebDefaults.tiktok_app_url + f"/@{unique_id}/live",
                base_params=False,
            )
            return self.parse_room_id(response.text)
        except (FailedParseRoomIdError, HTTPError):
            from TikTokLive.client.web.routes.fetch_room_id_api import FetchRoomIdAPIRoute
            return str(await FetchRoomIdAPIRoute(web=self._web)(unique_id))

    @classmethod
    def parse_room_id(cls, html: str) -> str:
        """
        Parse the room ID from livestream HTML

        :param html: The HTML to parse from https://tiktok.com/@<unique_id>/live
        :return: The user's room id
        :raises: UserOfflineError if the user is offline
        :raises: FailedParseRoomIdError if the user does not exist

        """

        # Method 1: SIGI_STATE
        sigi_match: Optional[re.Match[str]] = cls.SIGI_PATTERN.search(html)
        if sigi_match:
            try:
                sigi_state: dict = json.loads(sigi_match.group(1))
                if sigi_state.get('LiveRoom'):
                    room_data: dict = sigi_state["LiveRoom"]["liveRoomUserInfo"]["user"]
                    
                    # User is offline
                    if room_data.get('status') == 4:
                        raise UserOfflineError("The requested TikTok LIVE user is offline.")
                        
                    if room_data.get('roomId'):
                        return room_data['roomId']
            except UserOfflineError:
                raise
            except (JSONDecodeError, KeyError, TypeError, AttributeError):
                pass

        # Method 2: __UNIVERSAL_DATA_FOR_REHYDRATION__
        uni_match: Optional[re.Match[str]] = cls.UNIVERSAL_PATTERN.search(html)
        if uni_match:
            try:
                uni_data: dict = json.loads(uni_match.group(1))
                # Navigate to find room ID in universal data
                # Structure varies, but typically:
                # __DEFAULT_SCOPE__ -> webapp.user-detail -> userInfo -> user -> roomId
                user_info = (
                    uni_data
                    .get('__DEFAULT_SCOPE__', {})
                    .get('webapp.user-detail', {})
                    .get('userInfo', {})
                    .get('user', {})
                )
                
                if user_info.get('roomId'):
                    # Check status if available (optional, but good practice)
                    if user_info.get('status') == 4:
                        raise UserOfflineError("The requested TikTok LIVE user is offline.")
                    return user_info.get('roomId')
            except UserOfflineError:
                raise
            except (JSONDecodeError, KeyError, TypeError, AttributeError):
                pass

        # If we reach here, we failed to parse from HTML
        raise FailedParseRoomIdError("Failed to parse room ID from HTML using known patterns.")
