"""Bounded, read-only PirateTok experiment; never installs or changes the service.

Only counts/timings are saved. Cookie values, connection URLs, message IDs,
comment text and user identity never leave process memory. ``fresh_comments``
requires a non-history message with a plausible server timestamp at or after
the current handshake (two seconds of clock tolerance), not just an open socket.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import hashlib
import importlib.metadata
import io
import json
import logging
import re
import resource
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

SOURCE_COMMIT = "ee836405ef122d7e2dd3403d99b91cb0d967977e"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MAX_PAYLOAD = 8 * 1024 * 1024
MAX_IDS = 100_000
DIAGNOSTIC_CODE = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
KNOWN_REJECTION_REASONS = {
    "device_blocked", "room_not_found", "room_not_live", "illegal_secret_key",
    "invalid_signature", "signature_expired", "invalid_param",
}


def _safe_code(result: dict, name: str, value) -> None:
    """Keep short protocol codes; never echo arbitrary server text or headers."""
    if not isinstance(value, str) or not value:
        return
    if DIAGNOSTIC_CODE.fullmatch(value):
        result[name] = value
    else:
        encoded = value.encode("utf-8", errors="replace")
        result[f"{name}_bytes"] = len(encoded)
        result[f"{name}_sha256"] = hashlib.sha256(encoded).hexdigest()


def safe_error(exc: Exception) -> dict:
    """Exception strings can contain credentials and must not be recorded."""
    result = {"type": type(exc).__name__}
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        result["cause_type"] = type(cause).__name__
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", getattr(exc, "status_code", None))
    if isinstance(status, int):
        result["http_status"] = status
    api_code = getattr(exc, "code", None)
    if isinstance(api_code, int):
        result["api_code"] = api_code
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)  # Legacy websockets exceptions.
    if headers is not None:
        with contextlib.suppress(Exception):
            for header, field in (
                ("Handshake-Msg", "handshake_reason"),
                ("Handshake-Status", "handshake_status"),
                ("X-TT-System-Error", "tt_system_error"),
            ):
                _safe_code(result, field, headers.get(header, ""))
            content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type in {"application/json", "application/octet-stream", "text/html", "text/plain"}:
                result["content_type"] = content_type
            elif content_type:
                _safe_code(result, "content_type", content_type)
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):
        result["response_body_bytes"] = len(body)
        result["response_body_sha256"] = hashlib.sha256(body).hexdigest()
        # Do not log body text, JSON messages, cookies, URLs, or arbitrary fields.
        # Parsing is bounded even if a rejection carries a large HTML document.
        if len(body) <= 8192:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                payload = json.loads(body)
                result["response_body_kind"] = "json"
                if isinstance(payload, dict):
                    for field in ("status_code", "error_code", "code"):
                        value = payload.get(field)
                        if isinstance(value, int) and not isinstance(value, bool):
                            result[f"response_{field}"] = value
                    for field in ("message", "msg", "error"):
                        value = payload.get(field)
                        if isinstance(value, str):
                            reason = value.strip().lower().replace(" ", "_")
                            if reason in KNOWN_REJECTION_REASONS:
                                result["response_reason"] = reason
    return result


def bounded_payload(payload: bytes) -> bytes:
    if payload.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            result = stream.read(MAX_PAYLOAD + 1)
    else:
        result = payload
    if len(result) > MAX_PAYLOAD:
        raise ValueError("oversized payload")
    return result


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return round(sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))], 3)


class IdWindow:
    def __init__(self):
        self.ids = OrderedDict()
        self.evictions = 0

    def add(self, key: tuple, generation: int) -> str | None:
        previous = self.ids.pop(key, None)
        self.ids[key] = generation
        if len(self.ids) > MAX_IDS:
            self.ids.popitem(last=False)
            self.evictions += 1
        if previous is None:
            return None
        return "same_connection" if previous == generation else "cross_reconnect"


def rss_mib() -> float:
    # Linux ru_maxrss is KiB; Darwin is bytes. This is the high-water mark.
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 2)


async def run(args) -> dict:
    source = Path(args.source_root).resolve()
    sys.path.insert(0, str(source))
    from piratetok_live.auth.ttwid import fetch_ttwid
    from piratetok_live.connection.frames import build_ack, build_enter_room, build_heartbeat
    from piratetok_live.connection.url import build_wss_url
    from piratetok_live.http.api import check_online
    from piratetok_live.proto.schema import WebcastChatMessage, WebcastPushFrame, WebcastResponse
    from websockets.asyncio.client import connect

    started = time.monotonic()
    deadline = started + args.seconds
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_commit_expected": SOURCE_COMMIT,
        "source_sha256": {},
        "dependencies": {},
        "requested_seconds": args.seconds,
        "room_alias_sha256": hashlib.sha256(args.room.encode()).hexdigest(),
        "path": "PirateTok anonymous ttwid + unsigned WSS; independent lifecycle supervision",
        "bootstrap_mode": args.bootstrap,
        "bootstrap": {}, "resolve": {}, "attempts": [], "faults": [],
        "totals": Counter(), "methods": Counter(), "comment_buckets_30s": Counter(),
        "resource_samples": [], "termination": "deadline",
        "limitations": [
            "No comparison source: missing upstream messages cannot be quantified.",
            "Fresh timestamps depend on server/client clock alignment and history flags.",
            "Observed reconnect delays exclude silent network-blackhole detection.",
            "Successful sample is not a long-term uptime guarantee.",
        ],
    }
    for relative in ("auth/ttwid.py", "http/api.py", "connection/url.py", "connection/frames.py", "proto/schema.py"):
        report["source_sha256"][relative] = hashlib.sha256((source / "piratetok_live" / relative).read_bytes()).hexdigest()
    for name in ("betterproto", "websockets", "curl_cffi"):
        report["dependencies"][name] = importlib.metadata.version(name)
    if args.bootstrap == "registered":
        from piratetok_bootstrap import PirateTokBootstrapError, register_bootstrap

        report["bootstrap_helper_sha256"] = hashlib.sha256(
            Path(__file__).with_name("piratetok_bootstrap.py").read_bytes()
        ).hexdigest()
    report["source_actual_path"] = str(Path(sys.modules["piratetok_live"].__file__).resolve().parent)
    if not Path(report["source_actual_path"]).is_relative_to(source):
        raise RuntimeError("source root was not used")
    raw_ids, comment_ids = IdWindow(), IdWindow()
    loop_lags = []
    current_ws = None
    pending_fault = None
    generation = 0

    def elapsed():
        return round(time.monotonic() - started, 3)

    def checkpoint():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(args.output)

    async def sampler():
        next_sample = started
        expected = time.monotonic() + 0.25
        while True:
            await asyncio.sleep(max(0, expected - time.monotonic()))
            now = time.monotonic()
            loop_lags.append(max(0, (now - expected) * 1000))
            expected = now + 0.25
            if now >= next_sample:
                report["resource_samples"].append({"at": elapsed(), "rss_high_water_mib": rss_mib()})
                checkpoint()
                print(json.dumps({"at": elapsed(), "attempts": len(report["attempts"]), "totals": report["totals"]}), flush=True)
                next_sample = now + 15

    async def inject_faults():
        nonlocal pending_fault
        for at in args.fault_at:
            await asyncio.sleep(max(0, started + at - time.monotonic()))
            fault = {"scheduled_at": at, "at": elapsed()}
            report["faults"].append(fault)
            if current_ws is None:
                fault["result"] = "skipped_no_open_socket"
                continue
            fault.update(result="transport_aborted", generation=generation)
            pending_fault = fault
            current_ws.transport.abort()
            print(json.dumps({"fault": fault}), flush=True)

    async def heartbeat(ws, room_id):
        while True:
            await asyncio.sleep(10)
            await ws.send(build_heartbeat(room_id))
            report["totals"]["heartbeats_sent"] += 1

    monitor = asyncio.create_task(sampler())
    faults_task = asyncio.create_task(inject_faults())
    try:
        # One fixed UA, one anonymously issued ttwid, no proxy/IP/cookie rotation.
        # The upstream bootstrap helper tries target profile then @tiktok once.
        async with asyncio.timeout(max(0.1, deadline - time.monotonic())):
            user_agent = UA
            if args.bootstrap == "registered":
                stage = report["bootstrap"]
                stage["started_seconds"] = elapsed()
                try:
                    credentials = await asyncio.to_thread(
                        register_bootstrap, args.room, min(8.0, max(0.1, deadline - time.monotonic())),
                    )
                except PirateTokBootstrapError as exc:
                    stage.update(result="failed", completed_seconds=elapsed(), reason=exc.reason, diagnostics=exc.safe_details)
                    report["termination"] = "bootstrap_failed"
                    return report
                cookie_header = credentials.cookie
                user_agent = credentials.user_agent
                room_id = credentials.room_id
                stage.update(result="ok", completed_seconds=elapsed(), cookie_present=True, diagnostics=credentials.diagnostics)
                report["resolve"] = {"result": "ok", "included_in_bootstrap": True}
            else:
                stage = report["resolve"]
                stage["started_seconds"] = elapsed()
                try:
                    room = await asyncio.to_thread(check_online, args.room, 8.0, user_agent=UA, language="en", region="US")
                    room_id = room.room_id
                    stage.update(result="ok", completed_seconds=elapsed())
                except Exception as exc:
                    stage.update(result="failed", completed_seconds=elapsed(), error=safe_error(exc))
                    report["termination"] = "resolve_failed"
                    return report
                stage = report["bootstrap"]
                stage["started_seconds"] = elapsed()
                try:
                    ttwid = await asyncio.to_thread(fetch_ttwid, 8.0, username=args.room)
                    cookie_header = f"ttwid={ttwid}"
                    stage.update(result="ok", completed_seconds=elapsed(), cookie_present=bool(ttwid))
                except Exception as exc:
                    stage.update(result="failed", completed_seconds=elapsed(), error=safe_error(exc))
                    report["termination"] = "bootstrap_failed"
                    return report

            url = build_wss_url("webcast-ws.tiktok.com", room_id, "en", "US")
            if args.bootstrap == "registered":
                parts = urlsplit(url)
                query = parse_qs(parts.query)
                query["browser_platform"] = ["MacIntel"]
                query["browser_version"] = [user_agent.removeprefix("Mozilla/")]
                url = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
            params = parse_qs(urlsplit(url).query)
            signature_keys = {"signature", "x-bogus", "x-gnarly", "x-signature"}
            if signature_keys.intersection(key.lower() for key in params):
                raise RuntimeError("signed URL is not permitted in this experiment")
            report["url_signature_parameters_present"] = False
            report["wss_host"] = urlsplit(url).hostname
            unexpected_failures = 0
            while time.monotonic() < deadline:
                generation += 1
                attempt = {"generation": generation, "started_seconds": elapsed(), "counts": Counter()}
                report["attempts"].append(attempt)
                heartbeat_task = None
                fault_before = pending_fault
                try:
                    async with connect(
                        url, additional_headers={
                            "Cookie": cookie_header, "Origin": "https://www.tiktok.com",
                            "Referer": "https://www.tiktok.com/", "Accept-Language": "en-US,en;q=0.9",
                            "Cache-Control": "no-cache",
                        }, user_agent_header=user_agent, proxy=None, open_timeout=8,
                        # TikTok uses protobuf heartbeats and may not answer RFC
                        # WebSocket Ping; requiring Pong would create false drops.
                        close_timeout=1, ping_interval=None, ping_timeout=None,
                        max_size=MAX_PAYLOAD, max_queue=32,
                    ) as ws:
                        current_ws = ws
                        handshake_wall = time.time()
                        ready_at = elapsed()
                        attempt.update(handshake_ready_seconds=ready_at, handshake_duration_seconds=round(ready_at - attempt["started_seconds"], 3))
                        if fault_before is not None:
                            fault_before.setdefault("handshake_recovery_seconds", round(ready_at - fault_before["at"], 3))
                            fault_before.setdefault("recovered_generation", generation)
                        print(json.dumps({"generation": generation, "handshake_ready_seconds": ready_at}), flush=True)
                        await ws.send(build_heartbeat(room_id))
                        await ws.send(build_enter_room(room_id))
                        report["totals"]["heartbeats_sent"] += 1
                        report["totals"]["enter_room_sent"] += 1
                        heartbeat_task = asyncio.create_task(heartbeat(ws, room_id))
                        last_frame = time.monotonic()
                        while time.monotonic() < deadline:
                            if heartbeat_task.done():
                                heartbeat_task.result()
                            try:
                                raw = await asyncio.wait_for(ws.recv(), min(1, max(0.01, deadline - time.monotonic())))
                            except TimeoutError:
                                if time.monotonic() - last_frame >= 45:
                                    raise TimeoutError("upstream frame stale")
                                continue
                            last_frame = time.monotonic()
                            at = elapsed()
                            attempt.setdefault("first_frame_seconds", at)
                            attempt["last_frame_seconds"] = at
                            report["totals"]["raw_frames"] += 1
                            if not isinstance(raw, bytes):
                                report["totals"]["text_frames"] += 1
                                continue
                            frame = WebcastPushFrame().parse(raw)
                            if frame.payload_type != "msg":
                                report["totals"]["non_message_frames"] += 1
                                continue
                            response = WebcastResponse().parse(bounded_payload(frame.payload))
                            if response.needs_ack:
                                await ws.send(build_ack(frame.log_id, response.internal_ext))
                                report["totals"]["acks_sent"] += 1
                            for message in response.messages:
                                method = message.method if re.fullmatch(r"[A-Za-z0-9_]{1,100}", message.method) else "unknown_method"
                                report["methods"][method] += 1
                                report["totals"]["raw_messages"] += 1
                                attempt["counts"]["raw_messages"] += 1
                                attempt.setdefault("first_message_seconds", at)
                                attempt["last_message_seconds"] = at
                                if fault_before is not None:
                                    fault_before.setdefault("message_recovery_seconds", round(at - fault_before["at"], 3))
                                if message.msg_id > 0:
                                    duplicate = raw_ids.add((method, message.msg_id), generation)
                                    if duplicate:
                                        report["totals"][f"raw_duplicate_{duplicate}"] += 1
                                else:
                                    report["totals"]["raw_missing_id"] += 1
                                if method != "WebcastChatMessage":
                                    continue
                                chat = WebcastChatMessage().parse(message.payload)
                                msg_id = message.msg_id if message.msg_id > 0 else chat.common.msg_id
                                report["totals"]["comments"] += 1
                                attempt["counts"]["comments"] += 1
                                attempt.setdefault("first_comment_seconds", at)
                                attempt["last_comment_seconds"] = at
                                report["comment_buckets_30s"][str(int(at // 30))] += 1
                                duplicate = None
                                if msg_id > 0:
                                    duplicate = comment_ids.add((room_id, msg_id), generation)
                                    if duplicate:
                                        report["totals"][f"comment_duplicate_{duplicate}"] += 1
                                        attempt["counts"][f"comment_duplicate_{duplicate}"] += 1
                                else:
                                    report["totals"]["comments_missing_id"] += 1
                                report["totals"]["comments_flagged_history"] += int(message.is_history)
                                timestamp = chat.common.create_time
                                if timestamp > 10**12:
                                    timestamp /= 1000
                                # Require valid native ID + unique timestamped message.
                                fresh = msg_id > 0 and not duplicate and not message.is_history and handshake_wall - 2 <= timestamp <= time.time() + 5
                                if fresh:
                                    report["totals"]["fresh_comments"] += 1
                                    attempt["counts"]["fresh_comments"] += 1
                                    attempt.setdefault("first_fresh_comment_seconds", at)
                                    attempt["last_fresh_comment_seconds"] = at
                                    if fault_before is not None:
                                        fault_before.setdefault("fresh_comment_recovery_seconds", round(at - fault_before["at"], 3))
                                if at > ready_at + 5 and msg_id > 0 and not duplicate:
                                    attempt["counts"]["unique_comments_after_first_5s"] += 1
                        attempt["ended_by"] = "deadline"
                except asyncio.CancelledError:
                    attempt["ended_by"] = "deadline_or_cancel"
                    raise
                except Exception as exc:
                    error = safe_error(exc)
                    attempt["error"] = error
                    forced = pending_fault is not None and pending_fault.get("generation") == generation
                    attempt["ended_by"] = "injected_transport_abort" if forced else "unexpected_failure"
                    if not forced:
                        unexpected_failures += 1
                    # Stop on platform refusal. Never rotate identity to bypass it.
                    if error.get("http_status") in {401, 403, 415, 429} or error.get("handshake_reason") == "DEVICE_BLOCKED":
                        report["termination"] = "platform_rejected"
                        break
                    if unexpected_failures >= 2:
                        report["termination"] = "retry_budget_exhausted"
                        break
                finally:
                    current_ws = None
                    if heartbeat_task:
                        heartbeat_task.cancel()
                        await asyncio.gather(heartbeat_task, return_exceptions=True)
                    attempt["ended_seconds"] = elapsed()
                    checkpoint()
                if time.monotonic() < deadline:
                    await asyncio.sleep(0.2 if attempt.get("ended_by") == "injected_transport_abort" else 2)
    except TimeoutError:
        report["termination"] = "deadline"
    except Exception as exc:
        report["termination"] = "probe_error"
        report["probe_error"] = safe_error(exc)
    finally:
        monitor.cancel()
        faults_task.cancel()
        await asyncio.gather(monitor, faults_task, return_exceptions=True)
        report["observed_seconds"] = elapsed()
        report["rss_high_water_mib"] = rss_mib()
        report["loop_lag_ms"] = {"p50": percentile(loop_lags, 0.5), "p95": percentile(loop_lags, 0.95), "max": round(max(loop_lags, default=0), 3)}
        report["id_window_evictions"] = {"raw": raw_ids.evictions, "comments": comment_ids.evictions}
        report["injected_faults_recovered_with_fresh_comment"] = sum("fresh_comment_recovery_seconds" in f for f in report["faults"])
        report["assessment"] = "fresh_comments_observed_review_metrics" if report["totals"]["fresh_comments"] else "no_fresh_comments_stability_not_demonstrated"
        checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--bootstrap", choices=("profile", "registered"), default="profile")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--fault-at", type=float, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.room = args.room.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,64}", args.room):
        parser.error("--room must be a public TikTok username")
    if not 1 <= args.seconds <= 1800:
        parser.error("--seconds must be between 1 and 1800")
    args.fault_at = sorted(set(args.fault_at))
    if len(args.fault_at) > 3 or any(not 0 < at < args.seconds for at in args.fault_at):
        parser.error("provide at most three --fault-at values inside the run duration")
    logging.disable(logging.CRITICAL)
    report = asyncio.run(run(args))
    print(json.dumps({"termination": report["termination"], "assessment": report["assessment"], "observed_seconds": report["observed_seconds"], "totals": report["totals"], "report": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
