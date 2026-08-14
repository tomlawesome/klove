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

from klove.adapters.moonraker.start import (
    MoonrakerStartTransport,
    _observation,
    _response_document,
)
from klove.config import PrinterConfig
from klove.domain.models import JobHistoryStatus, JobIdentitySnapshot, PrinterPhase
from klove.domain.upload import MoonrakerGcodeMetadata, RemoteFileDigest
from klove.errors import StartTransportError

from ..start_helpers import PATH, history_job, verified_upload

API_KEY = "m" * 32


def live_result(
    state: str = "printing",
    *,
    filename: str = PATH,
) -> dict[str, Any]:
    return {
        "eventtime": 10,
        "status": {
            "print_stats": {"filename": filename, "state": state},
            "virtual_sdcard": {
                "file_position": 100 if state != "standby" else 0,
                "is_active": state == "printing",
            },
            "webhooks": {"state": "ready"},
        },
    }


def history_result(
    state: str = "printing",
    *,
    job_id: str = "000002",
    filename: str = PATH,
) -> dict[str, Any]:
    statuses = {
        "printing": JobHistoryStatus.IN_PROGRESS,
        "paused": JobHistoryStatus.IN_PROGRESS,
        "complete": JobHistoryStatus.COMPLETED,
        "cancelled": JobHistoryStatus.CANCELLED,
        "error": JobHistoryStatus.ERROR,
    }
    job = history_job(
        job_id=job_id,
        filename=filename,
        status=statuses[state],
    )
    return {"count": 1, "jobs": [job.model_dump(mode="json")]}


def config(endpoint: str) -> PrinterConfig:
    return PrinterConfig(
        id="voron",
        uuid="11111111-1111-4111-8111-111111111111",
        endpoint=endpoint,
        api_key_file=Path("unused"),
        allow_insecure_http=True,
    )


@pytest.mark.asyncio
async def test_transport_queries_and_dispatches_only_exact_typed_start() -> None:
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
        transport = MoonrakerStartTransport(
            config(str(server.make_url(""))),
            API_KEY,
            session,
            request_timeout_seconds=1,
        )
        observed = await transport.query()
        await transport.dispatch(PATH)

    assert observed.phase is PrinterPhase.PRINTING
    assert observed.latest_job == history_job()
    assert [request[0]["method"] for request in requests] == [
        "server.history.list",
        "printer.objects.query",
        "server.history.list",
        "printer.print.start",
    ]
    assert requests[0][0]["params"] == {"limit": 1, "start": 0, "order": "desc"}
    assert requests[1][0]["params"] == {
        "objects": {
            "print_stats": ["filename", "state"],
            "virtual_sdcard": ["file_position", "is_active"],
            "webhooks": ["state"],
        }
    }
    assert requests[3][0]["params"] == {"filename": PATH}
    assert [request[0]["id"] for request in requests] == [1, 2, 3, 4]
    assert all(key == API_KEY for _request, key in requests)


@pytest.mark.asyncio
async def test_file_reverification_methods_delegate_to_bounded_transport() -> None:
    verified = verified_upload()
    calls: list[tuple[object, ...]] = []

    class Files:
        async def metadata(self, path: str) -> MoonrakerGcodeMetadata:
            calls.append(("metadata", path))
            return verified.metadata

        async def download(self, path: str, expected_size: int) -> RemoteFileDigest:
            calls.append(("download", path, expected_size))
            return verified.remote_file

    async with aiohttp.ClientSession() as session:
        transport = MoonrakerStartTransport(
            config("http://127.0.0.1:7125"),
            API_KEY,
            session,
            request_timeout_seconds=1,
        )
        transport._files = Files()  # type: ignore[assignment]
        assert await transport.metadata(PATH) == verified.metadata
        assert await transport.download(PATH, 123) == verified.remote_file

    assert calls == [("metadata", PATH), ("download", PATH, 123)]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, True])
async def test_dispatch_requires_exact_ok_result(result: object) -> None:
    async def jsonrpc(request: web.Request) -> web.Response:
        document = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": document["id"], "result": result})

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerStartTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(StartTransportError):
            await transport.dispatch(PATH)


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
        transport = MoonrakerStartTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(StartTransportError):
            await transport.dispatch(PATH)


@pytest.mark.asyncio
async def test_invalid_json_and_client_errors_are_bounded() -> None:
    async def jsonrpc(_request: web.Request) -> web.Response:
        return web.Response(body=b"{", content_type="application/json")

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerStartTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(StartTransportError):
            await transport.dispatch(PATH)

    class BrokenSession:
        def post(self, *_args: object, **_kwargs: object) -> object:
            raise aiohttp.ClientError("secret remote detail")

    transport._session = BrokenSession()  # type: ignore[assignment]
    with pytest.raises(StartTransportError):
        await transport.dispatch(PATH)


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
    assert await _response_document(FakeResponse(charset="UTF-8")) == {"ok": True}  # type: ignore[arg-type]
    for response in (
        FakeResponse(status=500),
        FakeResponse(content_types=[]),
        FakeResponse(content_types=["application/json", "application/json"]),
        FakeResponse(parsed_content_type="text/plain"),
        FakeResponse(charset="iso-8859-1"),
        FakeResponse(body=b"x" * (64 * 1024 + 1)),
    ):
        with pytest.raises(StartTransportError):
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
def test_observation_maps_exact_consistent_evidence(state: str, phase: PrinterPhase) -> None:
    filename = "" if state == "standby" else PATH
    latest = None if state == "standby" else _job_for_state(state)
    observed = _observation(live_result(state, filename=filename), latest)
    assert observed.phase is phase
    assert observed.filename == filename


def _job_for_state(state: str) -> JobIdentitySnapshot:
    return JobIdentitySnapshot.model_validate(history_result(state)["jobs"][0])


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
def test_error_phase_accepts_pinned_terminal_history_states(
    status: JobHistoryStatus,
) -> None:
    latest = history_job(status=status)
    assert _observation(live_result("error"), latest).phase is PrinterPhase.ERROR


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
        mutate(("status", "print_stats"), {}),
        mutate(("status", "virtual_sdcard"), {}),
        mutate(("status", "webhooks"), {}),
        mutate(("status", "webhooks", "state"), "shutdown"),
        mutate(("status", "print_stats", "state"), 1),
        mutate(("status", "print_stats", "state"), "unknown"),
        mutate(("status", "print_stats", "filename"), 1),
        mutate(("status", "print_stats", "filename"), ""),
        mutate(("status", "virtual_sdcard", "file_position"), True),
        mutate(("status", "virtual_sdcard", "file_position"), 1.5),
        mutate(("status", "virtual_sdcard", "file_position"), -1),
        mutate(("status", "virtual_sdcard", "is_active"), "true"),
        mutate(("status", "virtual_sdcard", "is_active"), False),
    ],
)
def test_observation_rejects_every_malformed_or_contradictory_field(value: object) -> None:
    with pytest.raises(StartTransportError):
        _observation(value, history_job())


@pytest.mark.parametrize(
    "job",
    [
        None,
        history_job(filename="other.gcode"),
        history_job(status=JobHistoryStatus.COMPLETED),
    ],
)
def test_active_observation_requires_matching_history_identity(
    job: JobIdentitySnapshot | None,
) -> None:
    with pytest.raises(StartTransportError):
        _observation(live_result(), job)


@pytest.mark.asyncio
async def test_query_denies_when_history_identity_never_stabilizes() -> None:
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
        transport = MoonrakerStartTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(StartTransportError):
            await transport.query()

    assert history_calls == 6


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
        transport = MoonrakerStartTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        observed = await transport.query()

    assert history_calls == 4
    assert observed.latest_job is not None
    assert observed.latest_job.job_id == "000002"


@pytest.mark.asyncio
async def test_query_maps_malformed_history_to_bounded_transport_error() -> None:
    async def jsonrpc(request: web.Request) -> web.Response:
        document = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": document["id"], "result": {}})

    application = web.Application()
    application.router.add_post("/server/jsonrpc", jsonrpc)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        transport = MoonrakerStartTransport(
            config(str(server.make_url(""))), API_KEY, session, request_timeout_seconds=1
        )
        with pytest.raises(StartTransportError):
            await transport.query()
