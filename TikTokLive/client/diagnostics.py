"""Bounded source diagnostics; snapshots contain counters, never chat or identity data."""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict


class CommentDiagnostics:
    """Identify repeated source messages without retaining chat payloads.

    One instance belongs to one SDK connection. The service owns cross-connection
    delivery deduplication. This observer never changes SDK event emission.
    """

    def __init__(self, max_entries: int = 4096, ttl: float = 300.0):
        if not isinstance(max_entries, int) or max_entries < 0:
            raise ValueError("max_entries must be a nonnegative integer")
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl must be a positive finite number")
        self.max_entries = max_entries
        self.ttl = ttl
        self._seen = OrderedDict()
        self._counts = {
            "signer_comments": 0,
            "websocket_comments": 0,
            "invalid_source_ids": 0,
            "signer_duplicates": 0,
            "signer_to_websocket_replays": 0,
            "websocket_duplicates": 0,
            "other_source_repeats": 0,
            "repeated_ids_payload_changed": 0,
        }

    def observe(self, room_id: int, message_id: int, *, initial: bool, payload: bytes) -> None:
        self._counts["signer_comments" if initial else "websocket_comments"] += 1
        if room_id <= 0 or message_id <= 0:
            self._counts["invalid_source_ids"] += 1
            return
        if not self.max_entries:
            return
        now = time.monotonic()
        while self._seen and next(iter(self._seen.values()))[2] < now - self.ttl:
            self._seen.popitem(last=False)
        source_key = (room_id, message_id)
        # Store a digest, never the protobuf, comment text, username, or user ID.
        digest = hashlib.sha256(payload).digest()
        previous = self._seen.get(source_key)
        if previous is not None:
            was_initial, previous_digest, _ = previous
            if was_initial and initial:
                category = "signer_duplicates"
            elif was_initial:
                category = "signer_to_websocket_replays"
            elif not initial:
                category = "websocket_duplicates"
            else:
                category = "other_source_repeats"
            self._counts[category] += 1
            if digest != previous_digest:
                self._counts["repeated_ids_payload_changed"] += 1
            return
        self._seen[source_key] = (initial, digest, now)
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)

    def snapshot(self) -> dict[str, int]:
        return {**self._counts, "tracked_source_ids": len(self._seen)}
