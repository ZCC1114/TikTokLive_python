from __future__ import annotations

import time
from email.utils import parsedate_to_datetime


def retry_after_seconds(value: str | None, default: float = 60) -> float:
    """Honor both HTTP Retry-After forms without shortening server cooldowns."""
    if not isinstance(value, str) or len(value) > 128:
        return default
    value = value.strip()
    if value.isascii() and value.isdigit():
        return max(1, int(value))
    try:
        return max(1, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return default


class UpstreamError(ConnectionError):
    """Only safe, fixed protocol reasons cross the transport boundary."""

    def __init__(self, reason: str, *, terminal: bool = False, retry_after: float | None = None,
                 refresh_room: bool = False):
        self.reason = reason
        self.terminal = terminal
        self.retry_after = retry_after
        self.refresh_room = refresh_room
        super().__init__(reason)
