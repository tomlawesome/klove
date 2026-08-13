from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict

from klove.adapters.moonraker.control import (
    MoonrakerControlTransport,
    _live_state,
    _response_document,
)
from klove.config import PrinterConfig
from klove.domain.control import ControlOperation, LiveControlState
from klove.domain.models import JobHistoryStatus, JobIdentitySnapshot, PrinterPhase
from klove.errors import ControlTransportError

API_KEY = "m" * 32


def live_result(state: str = "printing") -> dict[str, Any]:
    return {
        "eventtime": 10,
        "status": {
            "pause_resume": {"is_paused": state == "paused"},
            "print_stats": {"filename": "job.gcode", "state": state},
            "virtual_sdcard": {
                "file_position": 100,
                "is_active": state == "printing",
            },
            "webhooks": {"state": "ready"},
        },
    }


def history_job(state: str = "printing", job_id: str = "000001") -> JobIdentitySnapshot:
    statuses = {
        "standby": JobHistoryStatus.COMPLETED,
        "printing": JobHistoryStatus.IN_PROGRESS,
        "paused": JobHistoryStatus.IN_PROGRESS,
        "complete": JobHistoryStatus.COMPLETED,
        "cancelled": JobHistoryStatus.CANCELLED,
        "error": JobHistoryStatus.ERROR,
    }
    return JobIdentitySnapshot(
        job_id=job_id,
        filename="job.gcode",
        start_time=1_700_000_000.0,
        status=statuses[state],
    )


def history_result(state: str = "printing", job_id: str = "000001") -> dict[str, Any]:
    return {"count": 1, "jobs": [history_job(state, job_id).model_dump(mode="json")]}


def config(endpoint: str) -> PrinterConfig:
    return PrinterConfig(
        id="voron",
        endpoint=endpoint,
        api_key_file=Path("unused"),
        allow_insecure_http=True,
    )


@pytest.mark.asyncio
async def test_transport_queries_and_dispatches_only_dedicated_methods() -> None:
    requests: list[tuple[dict[str, Any], str | None]] = []

    async def jsonrpc(request: web.Request) -> web.Response:
        document = await request.json()
        requests.append((document, request.headers.get("X-Api-Key")))
        if document["method"] == "printer.objects.query":
            result: object = live_result()
        elif document["method"] == "server.history.list":
            result = history_result()
        else:
            result = "ok"
        return web.json_response({"jsonrpc": "2.0", "id": document["id"], "result": result})

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerControlTransport(
            config(str(server.make_url(""))),
            API_KEY,
            session,
            request_timeout_seconds=1,
        )
        assert await transport.query() == LiveControlState(
            10.0,
            PrinterPhase.PRINTING,
            "000001",
            1_700_000_000.0,
            "job.gcode",
            100,
        )
        for operation in ControlOperation:
            await transport.dispatch(operation)

    assert [request[0]["method"] for request in requests] == [
        "server.history.list",
        "printer.objects.query",
        "server.history.list",
        "printer.print.pause",
        "printer.print.resume",
        "printer.print.cancel",
    ]
    assert requests[0][0]["params"] == {"limit": 1, "start": 0, "order": "desc"}
    assert set(requests[1][0]["params"]["objects"]) == {
        "pause_resume",
        "print_stats",
        "virtual_sdcard",
        "webhooks",
    }
    assert all("params" not in request for request, _key in requests[3:])
    assert all(key == API_KEY for _request, key in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, True])
async def test_dispatch_requires_exact_ok_result(result: object) -> None:
    async def jsonrpc(request: web.Request) -> web.Response:
        document = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": document["id"], "result": result})

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerControlTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(ControlTransportError):
            await transport.dispatch(ControlOperation.PAUSE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"jsonrpc": "1.0", "id": 1, "result": "ok"},
        {"jsonrpc": "2.0", "id": 2, "result": "ok"},
        {"jsonrpc": "2.0", "id": True, "result": "ok"},
        {"jsonrpc": "2.0", "id": 1.0, "result": "ok"},
        {"jsonrpc": "2.0", "id": "1", "result": "ok"},
        {"jsonrpc": "2.0", "id": 1, "result": "ok", "extra": True},
        {"jsonrpc": "2.0", "id": 1, "error": {}},
    ],
)
async def test_rpc_envelope_must_match_exactly(document: object) -> None:
    async def jsonrpc(_request: web.Request) -> web.Response:
        return web.json_response(document)

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerControlTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(ControlTransportError):
            await transport.dispatch(ControlOperation.PAUSE)


@pytest.mark.asyncio
async def test_invalid_json_and_client_errors_are_bounded() -> None:
    async def jsonrpc(_request: web.Request) -> web.Response:
        return web.Response(body=b"{", content_type="application/json")

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerControlTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(ControlTransportError):
            await transport.dispatch(ControlOperation.PAUSE)

    class BrokenSession:
        def post(self, *_args: object, **_kwargs: object) -> object:
            raise aiohttp.ClientError("secret remote detail")

    transport._session = BrokenSession()  # type: ignore[assignment]
    with pytest.raises(ControlTransportError):
        await transport.dispatch(ControlOperation.PAUSE)


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        body: bytes = b'{"ok":true}',
        *,
        status: int = 200,
        content_types: list[str] | None = None,
        parsed_content_type: str = "application/json",
        charset: str | None = None,
    ) -> None:
        self.status = status
        self.headers = CIMultiDict(
            ("Content-Type", value)
            for value in (content_types if content_types is not None else ["application/json"])
        )
        self.content_type = parsed_content_type
        self.charset = charset
        self.content = FakeContent([body])


@pytest.mark.asyncio
async def test_response_body_is_bounded_exact_utf8_json() -> None:
    assert await _response_document(FakeResponse()) == {"ok": True}  # type: ignore[arg-type]
    for response in (
        FakeResponse(status=500),
        FakeResponse(content_types=[]),
        FakeResponse(content_types=["application/json", "application/json"]),
        FakeResponse(parsed_content_type="text/plain"),
        FakeResponse(charset="iso-8859-1"),
        FakeResponse(body=b"x" * (64 * 1024 + 1)),
    ):
        with pytest.raises(ControlTransportError):
            await _response_document(response)  # type: ignore[arg-type]
    with pytest.raises(UnicodeDecodeError):
        await _response_document(FakeResponse(body=b"\xff"))  # type: ignore[arg-type]
    with pytest.raises(json.JSONDecodeError):
        await _response_document(FakeResponse(body=b'{"a":1,"a":2}'))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("state", "phase"),
    [
        ("standby", PrinterPhase.IDLE),
        ("printing", PrinterPhase.PRINTING),
        ("paused", PrinterPhase.PAUSED),
        ("complete", PrinterPhase.COMPLETED),
        ("cancelled", PrinterPhase.CANCELLED),
        ("error", PrinterPhase.ERROR),
    ],
)
def test_live_state_maps_exact_consistent_evidence(state: str, phase: PrinterPhase) -> None:
    live = _live_state(live_result(state), history_job(state))
    assert live.phase is phase
    assert live.job_status is history_job(state).status


@pytest.mark.parametrize(
    "status",
    [
        JobHistoryStatus.ERROR,
        JobHistoryStatus.KLIPPY_SHUTDOWN,
        JobHistoryStatus.KLIPPY_DISCONNECT,
        JobHistoryStatus.INTERRUPTED,
        JobHistoryStatus.SERVER_EXIT,
    ],
)
def test_error_phase_accepts_documented_and_pinned_terminal_history_states(
    status: JobHistoryStatus,
) -> None:
    identity = history_job("error").model_copy(update={"status": status})
    assert _live_state(live_result("error"), identity).phase is PrinterPhase.ERROR


def mutate(path: tuple[str, ...], value: object) -> dict[str, Any]:
    result = copy.deepcopy(live_result())
    target: dict[str, Any] = result
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return result


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"eventtime": 10, "status": {}, "extra": True},
        mutate(("eventtime",), True),
        mutate(("eventtime",), "10"),
        mutate(("eventtime",), float("inf")),
        mutate(("eventtime",), -1),
        mutate(("status",), []),
        mutate(("status", "webhooks"), {}),
        mutate(("status", "webhooks", "state"), "shutdown"),
        mutate(("status", "print_stats", "state"), 1),
        mutate(("status", "print_stats", "state"), "unknown"),
        mutate(("status", "print_stats", "filename"), 1),
        mutate(("status", "print_stats", "filename"), ""),
        mutate(("status", "virtual_sdcard", "file_position"), True),
        mutate(("status", "virtual_sdcard", "file_position"), 1.5),
        mutate(("status", "virtual_sdcard", "file_position"), -1),
        mutate(("status", "pause_resume", "is_paused"), "false"),
        mutate(("status", "virtual_sdcard", "is_active"), "true"),
        mutate(("status", "pause_resume", "is_paused"), True),
        mutate(("status", "virtual_sdcard", "is_active"), False),
    ],
)
def test_live_state_rejects_every_malformed_or_contradictory_field(value: object) -> None:
    with pytest.raises(ControlTransportError):
        _live_state(value, history_job())


@pytest.mark.parametrize(
    "job",
    [
        None,
        history_job().model_copy(update={"filename": "other.gcode"}),
    ],
)
def test_live_state_requires_matching_history_identity(
    job: JobIdentitySnapshot | None,
) -> None:
    with pytest.raises(ControlTransportError):
        _live_state(live_result(), job)


@pytest.mark.asyncio
async def test_query_denies_when_history_identity_changes_around_object_poll() -> None:
    history_calls = 0

    async def jsonrpc(request: web.Request) -> web.Response:
        nonlocal history_calls
        document = await request.json()
        if document["method"] == "printer.objects.query":
            result: object = live_result()
        else:
            history_calls += 1
            result = history_result(job_id=f"{history_calls:06X}")
        return web.json_response({"jsonrpc": "2.0", "id": document["id"], "result": result})

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerControlTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(ControlTransportError):
            await transport.query()


@pytest.mark.asyncio
async def test_query_retries_a_changing_history_bracket_until_coherent() -> None:
    history_calls = 0

    async def jsonrpc(request: web.Request) -> web.Response:
        nonlocal history_calls
        document = await request.json()
        if document["method"] == "printer.objects.query":
            result: object = live_result()
        else:
            history_calls += 1
            job_id = "000001" if history_calls == 1 else "000002"
            result = history_result(job_id=job_id)
        return web.json_response({"jsonrpc": "2.0", "id": document["id"], "result": result})

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerControlTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        live = await transport.query()

    assert history_calls == 4
    assert live.job_id == "000002"


@pytest.mark.asyncio
async def test_query_maps_malformed_history_to_a_bounded_transport_error() -> None:
    async def jsonrpc(request: web.Request) -> web.Response:
        document = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": document["id"], "result": {"jobs": []}})

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerControlTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(ControlTransportError):
            await transport.query()
