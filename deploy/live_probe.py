"""Observe a real room without saving comment text, user names, or signing data."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import websockets


async def probe(args):
    started = time.monotonic()
    deadline = started + args.seconds
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "room": args.room,
        "requested_seconds": args.seconds,
        "clients": [],
        "health_samples": [],
    }
    health_interval = getattr(args, "health_interval", 30)

    async def subscriber(index):
        stats = {
            "index": index, "comments": 0, "duplicate_ids": 0,
            "contract_valid_comments": 0, "invalid_contract_comments": 0,
            "statuses": [], "pong_ms": [], "errors": [],
        }
        report["clients"].append(stats)
        ids = set()
        pending_ping = None
        next_ping = 0.0
        url = args.base.replace("http://", "ws://").replace("https://", "wss://")
        try:
            async with websockets.connect(
                f"{url}/ws/{quote(args.room, safe='')}", open_timeout=15, close_timeout=5,
            ) as ws:
                stats["websocket_open_seconds"] = round(time.monotonic() - started, 3)
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now >= next_ping and pending_ping is None:
                        pending_ping = now
                        await ws.send("ping")
                        next_ping = now + 10
                    try:
                        message = await asyncio.wait_for(ws.recv(), min(1, max(0.01, deadline - now)))
                    except asyncio.TimeoutError:
                        if pending_ping is not None and time.monotonic() - pending_ping > 15:
                            stats["errors"].append("text_pong_timeout")
                            pending_ping = None
                        continue
                    if message == "pong":
                        if pending_ping is not None:
                            stats["pong_ms"].append(round((time.monotonic() - pending_ping) * 1000, 3))
                            pending_ping = None
                        continue
                    try:
                        data = json.loads(message)
                    except (ValueError, TypeError):
                        known = {"CONNECTING", "LIVING", "UPSTREAM_RECONNECTING", "LIVE_CONNECT_ERROR",
                                 "UPSTREAM_TIMEOUT", "METADATA_READY", "METADATA_UNAVAILABLE"}
                        value = message if message in known else "unexpected_text_message"
                        stats["statuses"].append({"at": round(time.monotonic() - started, 3), "value": value})
                        continue
                    if not isinstance(data, dict) or "danmuContent" not in data:
                        value = str(data) if type(data) is int and data in {0, 1, 2, 3} else "unexpected_json_message"
                        stats["statuses"].append({"at": round(time.monotonic() - started, 3), "value": value})
                        continue
                    stats.setdefault("first_comment_seconds", round(time.monotonic() - started, 3))
                    stats["last_comment_seconds"] = round(time.monotonic() - started, 3)
                    stats["comments"] += 1
                    fields = ("msgId", "dyMsgId", "danmuUserId", "username", "danmuUserName",
                              "danmuContent", "dyRoomId", "fansStatus")
                    try:
                        assert all(isinstance(data.get(field), str) for field in fields)
                        assert data["dyMsgId"].isdigit() and data["dyRoomId"].isdigit()
                        uuid.UUID(data["msgId"])
                        stats["contract_valid_comments"] += 1
                    except (AssertionError, ValueError, TypeError):
                        stats["invalid_contract_comments"] += 1
                        if "contract_violation" not in stats["errors"]:
                            stats["errors"].append("contract_violation")
                    key = (data.get("dyRoomId"), data.get("dyMsgId"))
                    if key[1] and key[1] != "0":
                        stats["duplicate_ids"] += int(key in ids)
                        ids.add(key)
                    stats.setdefault("message_fields", sorted(data))
        except Exception as exc:
            # Exception messages may contain request data; retain only the type.
            stats["errors"].append(type(exc).__name__)
        finally:
            stats["observed_seconds"] = round(time.monotonic() - started, 3)

    async def health():
        async with httpx.AsyncClient(timeout=5) as client:
            while time.monotonic() < deadline:
                sample = {"at": round(time.monotonic() - started, 3)}
                try:
                    health_base = getattr(args, "health_base", None) or args.base
                    response = await client.get(f"{health_base.rstrip('/')}/readyz")
                    sample.update(response.json())
                except Exception as exc:
                    sample["error"] = type(exc).__name__
                report["health_samples"].append(sample)
                if args.pid and sys.platform != "linux":
                    sample["rss_unavailable"] = "proc_not_available"
                elif args.pid:
                    try:
                        with open(f"/proc/{args.pid}/status") as status:
                            for line in status:
                                if line.startswith("VmRSS:"):
                                    sample["rss_kib"] = int(line.split()[1])
                    except OSError:
                        sample["process_missing"] = True
                print(json.dumps({"progress_seconds": sample["at"], "comments": [c["comments"] for c in report["clients"]], "health": sample}), flush=True)
                await asyncio.sleep(min(health_interval, max(0, deadline - time.monotonic())))

    await asyncio.gather(health(), *(subscriber(i) for i in range(args.clients)))
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    for stats in report["clients"]:
        pongs = sorted(stats["pong_ms"])
        stats["pong_summary"] = {
            "count": len(pongs),
            "median_ms": pongs[len(pongs) // 2] if pongs else None,
            "max_ms": max(pongs) if pongs else None,
        }
        stats["status_counts"] = dict(Counter(s["value"] for s in stats["statuses"]))
    if args.output:
        with open(args.output, "w") as output:
            json.dump(report, output, indent=2, ensure_ascii=False)
    print(json.dumps({"report": args.output, "clients": report["clients"]}, ensure_ascii=False), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("room")
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--health-base", help="Optional private health endpoint, for example an SSH loopback tunnel")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--clients", type=int, default=2)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--output", default="live-probe.json")
    parser.add_argument("--health-interval", type=float, default=30)
    options = parser.parse_args()
    if options.seconds < 1 or not 1 <= options.clients <= 10:
        parser.error("seconds must be positive and clients must be between 1 and 10")
    if not 0.1 <= options.health_interval <= 300:
        parser.error("health-interval must be between 0.1 and 300 seconds")
    options.base = options.base.rstrip("/")
    asyncio.run(probe(options))
