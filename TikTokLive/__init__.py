"""Protocol types remain importable without constructing the legacy signing stack."""

__all__ = ["TikTokLiveClient"]


def __getattr__(name):
    if name == "TikTokLiveClient":
        from .client.client import TikTokLiveClient

        return TikTokLiveClient
    raise AttributeError(name)
