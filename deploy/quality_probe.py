"""Run an isolated real-room probe and abort only its own upstream transport twice.

Usage: python -m deploy.quality_probe ROOM --seconds 240 --output quality-probe.json
The temporary service binds only 127.0.0.1:8766. No production process is signalled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from deploy.live_probe import probe
from live_service.app import create_app
from live_service.manager import ConnectionManager

HOST = "127.0.0.1"
PORT = 8766


async def run_quality_probe(args):
    # Binding first fails cleanly if another process owns this test port. Never
    # reuse, kill, or reconfigure an existing listener to make room for the test.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((HOST, PORT))
        listener.listen(128)
        listener.setblocking(False)
    except BaseException:
        listener.close()
        raise

    manager = ConnectionManager()
    app = create_app(manager)
    server = uvicorn.Server(uvicorn.Config(
        app, host=HOST, port=PORT, access_log=False, log_level="warning",
        lifespan="on", timeout_graceful_shutdown=10,
    ))
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "room": args.room,
        "requested_seconds": args.seconds,
        "isolated_listener": {"host": HOST, "port": PORT},
        "fault_injections": [],
    }
    server_task = asyncio.create_task(server.serve(sockets=[listener]), name="quality-probe-server")
    probe_task = fault_task = None
    started = None
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise RuntimeError("Temporary test server stopped before startup")
                await asyncio.sleep(0.05)

        started = time.monotonic()
        deadline = started + args.seconds
        key = manager._key(args.room)

        async def inject_faults():
            for index, scheduled in enumerate((args.seconds / 3, args.seconds * 2 / 3), 1):
                await asyncio.sleep(max(0, started + scheduled - time.monotonic()))
                fault = {"index": index, "scheduled_at": round(scheduled, 3)}
                report["fault_injections"].append(fault)
                # A room that has ended or failed to connect must not be forced
                # into another state merely to produce a successful test result.
                wait_until = min(deadline - 1, time.monotonic() + 15)
                room = client = transport = None
                while time.monotonic() < wait_until:
                    room = manager.rooms.get(key)
                    client = room.client if room is not None else None
                    upstream = getattr(client, "websocket", None) or getattr(getattr(client, "_ws", None), "ws", None)
                    transport = getattr(upstream, "transport", None)
                    if room and room.connected and transport and not transport.is_closing():
                        break
                    if room and room.ended:
                        break
                    await asyncio.sleep(0.1)
                else:
                    transport = None
                if not room or not room.connected or transport is None or transport.is_closing():
                    fault["skipped"] = "no_connected_upstream"
                    fault["checked_at"] = round(time.monotonic() - started, 3)
                    continue

                old_generation = room.generation
                fault["before"] = manager.health()
                fault["generation_before"] = old_generation
                aborted_at = time.monotonic()
                fault["at"] = round(aborted_at - started, 3)
                mode = "pause_inbound_reads" if index == 2 and getattr(args, "silent_fault", False) else "transport_abort"
                fault["mode"] = mode
                if mode == "pause_inbound_reads":
                    # This blackouts the collector's receive path while keeping
                    # its TCP socket open. It measures application silence
                    # detection, not kernel packet-loss or router recovery.
                    transport.pause_reading()
                else:
                    transport.abort()
                print(json.dumps({"fault_injected": index, "at": fault["at"], "generation": old_generation}), flush=True)
                # Record recovery independently of the 5-second health samples.
                # The per-subscriber LIVING statuses also expose wire recovery.
                recovery_deadline = min(deadline, aborted_at + 60)
                while time.monotonic() < recovery_deadline:
                    current = manager.rooms.get(key)
                    if current and current.generation > old_generation and current.connected:
                        fault["recovered_at"] = round(time.monotonic() - started, 3)
                        fault["recovery_seconds"] = round(time.monotonic() - aborted_at, 3)
                        fault["generation_after"] = current.generation
                        fault["after"] = manager.health()
                        print(json.dumps({"fault_recovered": index, "recovery_seconds": fault["recovery_seconds"]}), flush=True)
                        break
                    if current and current.ended:
                        fault["recovery_stopped"] = "stream_ended"
                        break
                    await asyncio.sleep(0.05)
                else:
                    fault["recovery_stopped"] = "not_recovered_within_observation"

        options = SimpleNamespace(
            room=args.room, seconds=args.seconds, clients=2,
            base=f"http://{HOST}:{PORT}", health_interval=5, pid=os.getpid(), output=None,
        )
        probe_task = asyncio.create_task(probe(options), name="quality-probe-clients")
        fault_task = asyncio.create_task(inject_faults(), name="quality-probe-faults")
        completed, _ = await asyncio.wait({server_task, probe_task, fault_task}, return_when=asyncio.FIRST_COMPLETED)
        if server_task in completed:
            await server_task
            raise RuntimeError("Temporary test server stopped during observation")
        # Successful fault injection normally finishes before the client probe.
        # Surface an injector error promptly and still shut everything down.
        if fault_task in completed:
            await fault_task
        report.update(await probe_task)
        await fault_task
        report["final_health"] = manager.health()
    except Exception as exc:
        report["run_error_type"] = type(exc).__name__
        # Error text can contain upstream URLs and signing material.
        print(json.dumps({"quality_probe_error": type(exc).__name__}), flush=True)
        raise
    finally:
        for task in (probe_task, fault_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (probe_task, fault_task) if task is not None), return_exceptions=True)
        if "final_health" not in report:
            report["final_health"] = manager.health()
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(server_task), 20)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server.force_exit = True
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        finally:
            with suppress(Exception):
                await manager.close()
            listener.close()
            report["shutdown_health"] = manager.health()
            if started is not None:
                report["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"quality_report": str(output), "faults": report["fault_injections"]}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("room")
    parser.add_argument("--seconds", type=int, default=240)
    parser.add_argument("--output", default="quality-probe.json")
    parser.add_argument("--silent-fault", action="store_true", help="Second fault pauses inbound reads on the test socket")
    options = parser.parse_args()
    if not 15 <= options.seconds <= 3600:
        parser.error("seconds must be between 15 and 3600")
    asyncio.run(run_quality_probe(options))
