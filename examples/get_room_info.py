from TikTokLive.client.web.routes.fetch_room_id_api import FetchRoomIdAPIRoute
import asyncio
import sys
import json

async def get_room_info(unique_id: str):
    try:
        # 直接用类方法
        data = await FetchRoomIdAPIRoute.fetch_user_room_data(unique_id)
        room_data = data.get("data", {})
        user = room_data.get("user", {})
        room = room_data.get("room", {})

        return {
            "nickname": user.get("nickname"),
            "avatar": user.get("avatar_thumb", {}).get("url_list", [None])[0],
            "room_title": room.get("title"),
            "room_cover": room.get("cover", {}).get("url_list", [None])[0],
            "room_id": room.get("id_str"),
            "is_live": room.get("status") == 2
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    if len(sys.argv) > 1:
        unique_id = sys.argv[1]
    else:
        unique_id = "popmart.th.shop2"

    result = asyncio.run(get_room_info(unique_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))
