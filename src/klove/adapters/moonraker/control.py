"""Strict Moonraker HTTP transport for typed print controls."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any, Protocol

import aiohttp

from klove.adapters.moonraker.history import decode_history_list
from klove.domain.control import ControlOperation, LiveControlState
from klove.domain.models import JobIdentitySnapshot, PrinterPhase
from klove.errors import ControlTransportError, ProtocolError

_MAX_RESPONSE_BYTES = 64 * 1024
_QUERY_OBJECTS = {
    "pause_resume": ["is_paused"],
    "print_stats": ["filename", "state"],
    "virtual_sdcard": ["file_position", "is_active"],
    "webhooks": ["state"],
}
_METHODS = {
    ControlOperation.PAUSE: "printer.print.pause",
    ControlOperation.RESUME: "printer.print.resume",
    ControlOperation.CANCEL: "printer.print.cancel",
}
_HISTORY_PARAMS: dict[str, object] = {"limit": 1, "start": 0, "order": "desc"}
_COHERENCE_ATTEMPTS = 3
_PHASES = {
    "standby": PrinterPhase.IDLE,
    "printing": PrinterPhase.PRINTING,
    "paused": PrinterPhase.PAUSED,
    "complete": PrinterPhase.COMPLETED,
    "cancelled": PrinterPhase.CANCELLED,
    "error": PrinterPhase.ERROR,
}


class MoonrakerControlConfig(Protocol):
    """Minimal immutable connection settings required by job control."""

    @property
    def endpoint(self) -> str: ...

    @property
    def verify_tls(self) -> bool: ...


class MoonrakerControlTransport:
    """Poll and actuate one explicitly configured Moonraker instance."""

    def __init__(
        self,
        config: MoonrakerControlConfig,
        api_key: str,
        session: aiohttp.ClientSession,
        *,
        request_timeout_seconds: float,
    ) -> None:
        self._url = f"{config.endpoint.rstrip('/')}/server/jsonrpc"
        self._api_key = api_key
        self._session = session
        self._verify_tls = config.verify_tls
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)
        self._request_id = 0

    async def query(self) -> LiveControlState:
        """Bracket current printer state with one exact history identity."""
        for _attempt in range(_COHERENCE_ATTEMPTS):
            history_before = _history_job(
                await self._rpc("server.history.list", dict(_HISTORY_PARAMS))
            )
            result = await self._rpc("printer.objects.query", {"objects": _QUERY_OBJECTS})
            history_after = _history_job(
                await self._rpc("server.history.list", dict(_HISTORY_PARAMS))
            )
            if history_before == history_after:
                return _live_state(result, history_after)
        raise ControlTransportError

    async def dispatch(self, operation: ControlOperation) -> None:
        """Send one parameter-free dedicated print-control method."""
        result = await self._rpc(_METHODS[operation])
        if result != "ok":
            raise ControlTransportError

    async def _rpc(self, method: str, params: dict[str, object] | None = None) -> object:
        self._request_id += 1
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._request_id,
        }
        if params is not None:
            request["params"] = params
        try:
            async with self._session.post(
                self._url,
                json=request,
                headers={"X-Api-Key": self._api_key},
                allow_redirects=False,
                ssl=self._verify_tls,
                timeout=self._timeout,
            ) as response:
                document = await _response_document(response)
        except (aiohttp.ClientError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlTransportError from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"jsonrpc", "id", "result"}
            or document.get("jsonrpc") != "2.0"
            or isinstance(document.get("id"), bool)
            or not isinstance(document.get("id"), int)
            or document.get("id") != self._request_id
        ):
            raise ControlTransportError
        return document["result"]


async def _response_document(response: aiohttp.ClientResponse) -> object:
    if response.status != 200:
        raise ControlTransportError
    content_types = response.headers.getall("Content-Type", [])
    if len(content_types) != 1 or response.content_type != "application/json":
        raise ControlTransportError
    if response.charset is not None and response.charset.casefold() != "utf-8":
        raise ControlTransportError
    body = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ControlTransportError
    return json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object member", key, 0)
        result[key] = value
    return result


def _live_state(value: object, job: JobIdentitySnapshot | None) -> LiveControlState:
    if not isinstance(value, dict) or set(value) != {"eventtime", "status"}:
        raise ControlTransportError
    eventtime = value["eventtime"]
    status = value["status"]
    if (
        isinstance(eventtime, bool)
        or not isinstance(eventtime, (int, float))
        or not math.isfinite(float(eventtime))
        or eventtime < 0
        or not isinstance(status, dict)
        or set(status) != set(_QUERY_OBJECTS)
    ):
        raise ControlTransportError
    pause_resume = _exact_object(status["pause_resume"], {"is_paused"})
    print_stats = _exact_object(status["print_stats"], {"filename", "state"})
    virtual_sdcard = _exact_object(status["virtual_sdcard"], {"file_position", "is_active"})
    webhooks = _exact_object(status["webhooks"], {"state"})
    raw_phase = print_stats["state"]
    filename = print_stats["filename"]
    file_position = virtual_sdcard["file_position"]
    is_paused = pause_resume["is_paused"]
    is_active = virtual_sdcard["is_active"]
    if (
        webhooks["state"] != "ready"
        or not isinstance(raw_phase, str)
        or raw_phase not in _PHASES
        or not isinstance(filename, str)
        or not filename
        or isinstance(file_position, bool)
        or not isinstance(file_position, int)
        or file_position < 0
        or not isinstance(is_paused, bool)
        or not isinstance(is_active, bool)
        or is_paused is not (raw_phase == "paused")
        or is_active is not (raw_phase == "printing")
    ):
        raise ControlTransportError
    phase = _PHASES[raw_phase]
    if job is None or job.filename != filename:
        raise ControlTransportError
    return LiveControlState(
        eventtime=float(eventtime),
        phase=phase,
        job_id=job.job_id,
        job_start_time=job.start_time,
        filename=filename,
        file_position=file_position,
        job_status=job.status,
    )


def _history_job(value: object) -> JobIdentitySnapshot | None:
    try:
        return decode_history_list(value)
    except ProtocolError as exc:
        raise ControlTransportError from exc


def _exact_object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ControlTransportError
    return value
