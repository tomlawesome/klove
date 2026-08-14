from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from klove.adapters.moonraker.client import MoonrakerMonitor
from klove.config import PrinterConfig
from klove.domain.models import PrinterPhase, PrinterSnapshot
from klove.errors import ProtocolError, StateEvidenceError
from klove.registry import PrinterRegistry

API_KEY = "m" * 32


def history_job(status: str = "in_progress") -> dict[str, object]:
    return {
        "job_id": "000001",
        "filename": "job.gcode",
        "start_time": 1_700_000_000.0,
        "status": status,
        "metadata": {},
    }


class RecordingRegistry(PrinterRegistry):
    def __init__(self) -> None:
        super().__init__(["voron"])
        self.history: list[PrinterSnapshot] = []

    async def replace(self, snapshot: PrinterSnapshot) -> None:
        await super().replace(snapshot)
        self.history.append(snapshot)


def config(endpoint: str) -> PrinterConfig:
    return PrinterConfig(
        id="voron",
        uuid="11111111-1111-4111-8111-111111111111",
        endpoint=endpoint,
        api_key_file=Path("unused"),
        allow_insecure_http=True,
    )


async def run_against(handler: Any) -> tuple[RecordingRegistry, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        connection = web.WebSocketResponse()
        await connection.prepare(request)
        await handler(connection, requests)
        return connection

    server = TestServer(web.Application())
    server.app.router.add_get("/websocket", websocket)
    async with server:
        registry = RecordingRegistry()
        async with aiohttp.ClientSession() as session:
            monitor = MoonrakerMonitor(config(str(server.make_url(""))), API_KEY, registry, session)
            with pytest.raises(ProtocolError, match="closed or sent a non-text"):
                await monitor.run_once()
    return registry, requests


async def reply(connection: web.WebSocketResponse, request: dict[str, Any], result: Any) -> None:
    await connection.send_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


@pytest.mark.asyncio
async def test_monitor_identifies_discovers_subscribes_and_invalidates_on_disconnect() -> None:
    async def handler(connection: web.WebSocketResponse, requests: list[dict[str, Any]]) -> None:
        history_calls = 0
        results = {
            "server.connection.identify": {"connection_id": 7},
            "server.info": {"klippy_connected": True, "klippy_state": "ready"},
            "printer.info": {"state": "ready"},
            "printer.objects.list": {
                "objects": [
                    "pause_resume",
                    "print_stats",
                    "virtual_sdcard",
                    "extruder",
                    "heater_bed",
                    "heater_generic chamber",
                    "fan",
                    "fan_generic aux",
                    "controller_fan electronics",
                    "heater_fan hotend",
                    "filament_switch_sensor runout",
                    "filament_motion_sensor encoder",
                ]
            },
            "server.history.list": {"count": 0, "jobs": []},
            "printer.objects.subscribe": {
                "eventtime": 10,
                "status": {
                    "pause_resume": {"is_paused": False},
                    "print_stats": {"state": "standby"},
                    "virtual_sdcard": {"is_active": False, "progress": 0},
                },
            },
        }
        async for message in connection:
            request = message.json()
            requests.append(request)
            await reply(connection, request, results[request["method"]])
            if request["method"] == "server.history.list":
                history_calls += 1
            if history_calls == 2:
                await connection.send_json(
                    {"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": [{}]}
                )
                await connection.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "notify_status_update",
                        "params": [
                            {
                                "print_stats": {"state": "printing"},
                                "virtual_sdcard": {"is_active": True},
                            },
                            11,
                        ],
                    }
                )
                await connection.send_json(
                    {"jsonrpc": "2.0", "method": "notify_klippy_shutdown", "params": []}
                )
                await connection.send_json(
                    {"jsonrpc": "2.0", "method": "notify_klippy_disconnected", "params": []}
                )
                await connection.close()

    registry, requests = await run_against(handler)

    identify = requests[0]
    assert identify["method"] == "server.connection.identify"
    assert identify["params"]["api_key"] == API_KEY
    assert [snapshot.phase for snapshot in registry.history] == [
        PrinterPhase.IDLE,
        PrinterPhase.PRINTING,
        PrinterPhase.NOT_READY,
        PrinterPhase.OFFLINE,
        PrinterPhase.OFFLINE,
    ]
    subscription = next(
        request for request in requests if request["method"] == "printer.objects.subscribe"
    )
    subscribed = subscription["params"]["objects"]
    assert subscribed["extruder"] == ["can_extrude", "power", "target", "temperature"]
    assert subscribed["fan"] == ["rpm", "speed"]
    assert subscribed["heater_generic chamber"] == ["power", "target", "temperature"]
    assert subscribed["filament_switch_sensor runout"] == ["enabled", "filament_detected"]


@pytest.mark.asyncio
async def test_monitor_waits_for_ready_then_rebuilds_all_evidence() -> None:
    server_info_calls = 0
    history_calls = 0

    async def handler(connection: web.WebSocketResponse, requests: list[dict[str, Any]]) -> None:
        nonlocal history_calls, server_info_calls
        async for message in connection:
            request = message.json()
            requests.append(request)
            method = request["method"]
            if method == "server.connection.identify":
                await reply(connection, request, {"connection_id": 8})
            elif method == "server.info":
                server_info_calls += 1
                ready = server_info_calls > 1
                await reply(
                    connection,
                    request,
                    {"klippy_connected": ready, "klippy_state": "ready" if ready else "startup"},
                )
                if not ready:
                    await connection.send_json(
                        {"jsonrpc": "2.0", "method": "notify_klippy_ready", "params": None}
                    )
            elif method == "printer.info":
                await reply(connection, request, {"state": "ready"})
            elif method == "printer.objects.list":
                await reply(
                    connection,
                    request,
                    {"objects": ["pause_resume", "print_stats", "virtual_sdcard"]},
                )
            elif method == "printer.objects.subscribe":
                await reply(
                    connection,
                    request,
                    {
                        "eventtime": 20,
                        "status": {
                            "pause_resume": {"is_paused": True},
                            "print_stats": {"state": "paused"},
                            "virtual_sdcard": {"is_active": False},
                        },
                    },
                )
            elif method == "server.history.list":
                history_calls += 1
                await reply(connection, request, {"count": 0, "jobs": []})
                if history_calls == 2:
                    await connection.close()

    registry, _requests = await run_against(handler)
    assert [snapshot.phase for snapshot in registry.history] == [
        PrinterPhase.NOT_READY,
        PrinterPhase.PAUSED,
        PrinterPhase.OFFLINE,
    ]


@pytest.mark.asyncio
async def test_monitor_bootstraps_and_rotates_exact_history_job_identity() -> None:
    async def handler(connection: web.WebSocketResponse, requests: list[dict[str, Any]]) -> None:
        history_calls = 0
        active = history_job()
        results = {
            "server.connection.identify": {"connection_id": 12},
            "server.info": {"klippy_connected": True, "klippy_state": "ready"},
            "printer.info": {"state": "ready"},
            "printer.objects.list": {"objects": ["pause_resume", "print_stats", "virtual_sdcard"]},
            "server.history.list": {"count": 1, "jobs": [active]},
            "printer.objects.subscribe": {
                "eventtime": 10,
                "status": {
                    "pause_resume": {"is_paused": False},
                    "print_stats": {"state": "printing", "filename": "job.gcode"},
                    "virtual_sdcard": {"is_active": True, "file_position": 100},
                },
            },
        }
        async for message in connection:
            request = message.json()
            requests.append(request)
            await reply(connection, request, results[request["method"]])
            if request["method"] == "server.connection.identify":
                await connection.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "notify_history_changed",
                        "params": [{"action": "added", "job": active}],
                    }
                )
            elif request["method"] == "server.history.list":
                history_calls += 1
                if history_calls == 2:
                    await connection.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "notify_history_changed",
                            "params": [{"action": "finished", "job": history_job("completed")}],
                        }
                    )
                    await connection.close()

    registry, _requests = await run_against(handler)

    printing, terminal, offline = registry.history
    assert printing.phase is PrinterPhase.PRINTING
    assert printing.job is not None and printing.job.status.value == "in_progress"
    assert terminal.phase is PrinterPhase.PRINTING
    assert terminal.job is not None and terminal.job.status.value == "completed"
    assert len({printing.state_token, terminal.state_token, offline.state_token}) == 3


@pytest.mark.asyncio
async def test_monitor_rejects_a_job_change_during_bootstrap() -> None:
    history_calls = 0

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        nonlocal history_calls
        connection = web.WebSocketResponse()
        await connection.prepare(request)
        async for message in connection:
            rpc = message.json()
            method = rpc["method"]
            if method == "server.connection.identify":
                result: object = {"connection_id": 13}
            elif method == "server.info":
                result = {"klippy_connected": True, "klippy_state": "ready"}
            elif method == "printer.info":
                result = {"state": "ready"}
            elif method == "printer.objects.list":
                result = {"objects": ["pause_resume", "print_stats", "virtual_sdcard"]}
            elif method == "server.history.list":
                history_calls += 1
                changed = history_job()
                changed["job_id"] = f"{history_calls:06X}"
                result = {"count": 1, "jobs": [changed]}
            else:
                result = {
                    "eventtime": 10,
                    "status": {
                        "pause_resume": {"is_paused": False},
                        "print_stats": {"state": "printing", "filename": "job.gcode"},
                        "virtual_sdcard": {"is_active": True, "file_position": 100},
                    },
                }
            await reply(connection, rpc, result)
        return connection

    server = TestServer(web.Application())
    server.app.router.add_get("/websocket", websocket)
    async with server:
        registry = RecordingRegistry()
        async with aiohttp.ClientSession() as session:
            monitor = MoonrakerMonitor(config(str(server.make_url(""))), API_KEY, registry, session)
            with pytest.raises(StateEvidenceError, match="history changed"):
                await monitor.run_once()

    assert history_calls == 2
    assert registry.history[-1].phase is PrinterPhase.OFFLINE


@pytest.mark.asyncio
async def test_missing_required_objects_are_visible_but_never_eligible() -> None:
    async def handler(connection: web.WebSocketResponse, requests: list[dict[str, Any]]) -> None:
        results = {
            "server.connection.identify": {"connection_id": 9},
            "server.info": {"klippy_connected": True, "klippy_state": "ready"},
            "printer.info": {"state": "ready"},
            "printer.objects.list": {"objects": ["print_stats"]},
        }
        async for message in connection:
            request = message.json()
            requests.append(request)
            await reply(connection, request, results[request["method"]])
            if request["method"] == "printer.objects.list":
                await connection.close()

    registry, requests = await run_against(handler)
    limited = registry.history[0]
    assert limited.phase is PrinterPhase.NOT_READY
    assert limited.capabilities is not None
    assert not limited.capabilities.dispatch_eligible
    assert all(request["method"] != "printer.objects.subscribe" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_result",
    [
        {},
        {"objects": "print_stats"},
        {"objects": [], "extra": True},
        {"objects": ["print_stats", {}]},
    ],
)
async def test_invalid_object_discovery_shape_fails_closed(object_result: dict[str, Any]) -> None:
    async def handler(connection: web.WebSocketResponse, requests: list[dict[str, Any]]) -> None:
        results = {
            "server.connection.identify": {"connection_id": 10},
            "server.info": {"klippy_connected": True, "klippy_state": "ready"},
            "printer.info": {"state": "ready"},
            "printer.objects.list": object_result,
        }
        async for message in connection:
            request = message.json()
            requests.append(request)
            await reply(connection, request, results[request["method"]])

    requests: list[dict[str, Any]] = []

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        connection = web.WebSocketResponse()
        await connection.prepare(request)
        await handler(connection, requests)
        return connection

    server = TestServer(web.Application())
    server.app.router.add_get("/websocket", websocket)
    async with server:
        registry = RecordingRegistry()
        async with aiohttp.ClientSession() as session:
            monitor = MoonrakerMonitor(config(str(server.make_url(""))), API_KEY, registry, session)
            with pytest.raises(ProtocolError, match=r"printer\.objects\.list"):
                await monitor.run_once()
    assert registry.history[-1].phase is PrinterPhase.OFFLINE


@pytest.mark.asyncio
async def test_unsolicited_response_after_bootstrap_is_rejected() -> None:
    async def handler(connection: web.WebSocketResponse, requests: list[dict[str, Any]]) -> None:
        history_calls = 0
        results = {
            "server.connection.identify": {"connection_id": 11},
            "server.info": {"klippy_connected": True, "klippy_state": "ready"},
            "printer.info": {"state": "ready"},
            "printer.objects.list": {"objects": ["pause_resume", "print_stats", "virtual_sdcard"]},
            "server.history.list": {"count": 0, "jobs": []},
            "printer.objects.subscribe": {
                "eventtime": 1,
                "status": {
                    "pause_resume": {"is_paused": False},
                    "print_stats": {"state": "standby"},
                    "virtual_sdcard": {"is_active": False},
                },
            },
        }
        async for message in connection:
            request = message.json()
            requests.append(request)
            await reply(connection, request, results[request["method"]])
            if request["method"] == "server.history.list":
                history_calls += 1
            if history_calls == 2:
                await connection.send_json({"jsonrpc": "2.0", "id": 999, "result": {}})

    requests: list[dict[str, Any]] = []

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        connection = web.WebSocketResponse()
        await connection.prepare(request)
        await handler(connection, requests)
        return connection

    server = TestServer(web.Application())
    server.app.router.add_get("/websocket", websocket)
    async with server:
        registry = RecordingRegistry()
        async with aiohttp.ClientSession() as session:
            monitor = MoonrakerMonitor(config(str(server.make_url(""))), API_KEY, registry, session)
            with pytest.raises(ProtocolError, match="unexpected JSON-RPC response"):
                await monitor.run_once()
