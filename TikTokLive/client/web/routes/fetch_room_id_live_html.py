import json
import re
from json import JSONDecodeError
from typing import Optional

from httpx import Response

from TikTokLive.client.errors import UserOfflineError, UserNotFoundError, TikTokLiveError
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

        # Get their livestream HTML
        response: Response = await self._web.get(
            url=WebDefaults.tiktok_app_url + f"/@{unique_id}/live",
            base_params=False
        )

        # Try to parse the room ID from the HTML
        try:
            return self.parse_room_id(response.text)
        except FailedParseRoomIdError:
            pass

        # Fallback: Use the API to fetch the room ID
        # Import here to avoid circular dependency
        from TikTokLive.client.web.routes.fetch_room_id_api import FetchRoomIdAPIRoute
        
        try:
            api_route = FetchRoomIdAPIRoute(web=self._web)
            return str(await api_route(unique_id))
        except Exception as ex:
            # If the API also fails, raise a UserNotFoundError with a clear message
            raise UserNotFoundError(
                unique_id,
                "Failed to retrieve room_id from both HTML and API. "
                "The user might be offline, blocked, or the page structure has changed."
            ) from ex

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
                        
                    return room_data.get('roomId')
            except JSONDecodeError:
                pass
            except Exception:
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
            except JSONDecodeError:
                pass
            except Exception:
                pass

        # If we reach here, we failed to parse from HTML
        raise FailedParseRoomIdError("Failed to parse room ID from HTML using known patterns.")
