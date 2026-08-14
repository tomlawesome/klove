"""Strict Moonraker transport for exact print start and reconciliation polls."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

import aiohttp

from klove.adapters.moonraker.history import decode_history_list
from klove.adapters.moonraker.upload import MoonrakerUploadTransport
from klove.config import PrinterConfig
from klove.domain.models import JobHistoryStatus, JobIdentitySnapshot, PrinterPhase
from klove.domain.start import StartObservation
from klove.domain.upload import MoonrakerGcodeMetadata, RemoteFileDigest
from klove.errors import ProtocolError, StartTransportError

_MAX_RESPONSE_BYTES = 64 * 1024
_COHERENCE_ATTEMPTS = 3
_QUERY_OBJECTS = {
    "print_stats": ["filename", "state"],
    "virtual_sdcard": ["file_position", "is_active"],
    "webhooks": ["state"],
}
_HISTORY_PARAMS: dict[str, object] = {"limit": 1, "start": 0, "order": "desc"}
_PHASES = {
    "standby": PrinterPhase.IDLE,
    "printing": PrinterPhase.PRINTING,
    "paused": PrinterPhase.PAUSED,
    "complete": PrinterPhase.COMPLETED,
    "cancelled": PrinterPhase.CANCELLED,
    "error": PrinterPhase.ERROR,
}


class MoonrakerStartTransport:
    """Reverify one remote file, poll coherent state, and start its exact path."""

    def __init__(
        self,
        config: PrinterConfig,
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
        self._files = MoonrakerUploadTransport(
            config,
            api_key,
            session,
            request_timeout_seconds=request_timeout_seconds,
        )

    async def metadata(self, path: str) -> MoonrakerGcodeMetadata | None:
        """Read exact current metadata through the bounded file transport."""
        return await self._files.metadata(path)

    async def download(self, path: str, expected_size: int) -> RemoteFileDigest:
        """Stream the exact current remote file through SHA-256."""
        return await self._files.download(path, expected_size)

    async def query(self) -> StartObservation:
        """Bracket live printer state with one coherent newest history identity."""
        for _attempt in range(_COHERENCE_ATTEMPTS):
            history_before = _history_job(
                await self._rpc("server.history.list", dict(_HISTORY_PARAMS))
            )
            result = await self._rpc("printer.objects.query", {"objects": _QUERY_OBJECTS})
            history_after = _history_job(
                await self._rpc("server.history.list", dict(_HISTORY_PARAMS))
            )
            if history_before == history_after:
                return _observation(result, history_after)
        raise StartTransportError

    async def dispatch(self, path: str) -> None:
        """Send one typed print-start method for the exact verified path."""
        result = await self._rpc("printer.print.start", {"filename": path})
        if result != "ok":
            raise StartTransportError

    async def _rpc(self, method: str, params: dict[str, object]) -> object:
        self._request_id += 1
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._request_id,
        }
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
            raise StartTransportError from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"jsonrpc", "id", "result"}
            or document.get("jsonrpc") != "2.0"
            or isinstance(document.get("id"), bool)
            or not isinstance(document.get("id"), int)
            or document.get("id") != self._request_id
        ):
            raise StartTransportError
        return document["result"]


async def _response_document(response: aiohttp.ClientResponse) -> object:
    if response.status != 200:
        raise StartTransportError
    content_types = response.headers.getall("Content-Type", [])
    if len(content_types) != 1 or response.content_type != "application/json":
        raise StartTransportError
    if response.charset is not None and response.charset.casefold() != "utf-8":
        raise StartTransportError
    body = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise StartTransportError
    return json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object member", key, 0)
        result[key] = value
    return result


def _observation(value: object, latest_job: JobIdentitySnapshot | None) -> StartObservation:
    if not isinstance(value, dict) or set(value) != {"eventtime", "status"}:
        raise StartTransportError
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
        raise StartTransportError
    print_stats = _exact_object(status["print_stats"], {"filename", "state"})
    virtual_sdcard = _exact_object(status["virtual_sdcard"], {"file_position", "is_active"})
    webhooks = _exact_object(status["webhooks"], {"state"})
    raw_phase = print_stats["state"]
    filename = print_stats["filename"]
    file_position = virtual_sdcard["file_position"]
    is_active = virtual_sdcard["is_active"]
    if (
        webhooks["state"] != "ready"
        or not isinstance(raw_phase, str)
        or raw_phase not in _PHASES
        or not isinstance(filename, str)
        or (raw_phase != "standby" and not filename)
        or isinstance(file_position, bool)
        or not isinstance(file_position, int)
        or file_position < 0
        or not isinstance(is_active, bool)
        or is_active is not (raw_phase == "printing")
    ):
        raise StartTransportError
    phase = _PHASES[raw_phase]
    if phase is not PrinterPhase.IDLE:
        if latest_job is None or latest_job.filename != filename:
            raise StartTransportError
        if not _status_matches_phase(latest_job.status, phase):
            raise StartTransportError
    return StartObservation(
        eventtime=float(eventtime),
        phase=phase,
        filename=filename,
        file_position=file_position,
        latest_job=latest_job,
    )


def _status_matches_phase(status: JobHistoryStatus, phase: PrinterPhase) -> bool:
    if phase in {PrinterPhase.PRINTING, PrinterPhase.PAUSED}:
        return status is JobHistoryStatus.IN_PROGRESS
    if phase is PrinterPhase.COMPLETED:
        return status is JobHistoryStatus.COMPLETED
    if phase is PrinterPhase.CANCELLED:
        return status is JobHistoryStatus.CANCELLED
    return phase is PrinterPhase.ERROR and status in {
        JobHistoryStatus.ERROR,
        JobHistoryStatus.KLIPPY_SHUTDOWN,
        JobHistoryStatus.KLIPPY_DISCONNECT,
        JobHistoryStatus.INTERRUPTED,
        JobHistoryStatus.SERVER_EXIT,
    }


def _history_job(value: object) -> JobIdentitySnapshot | None:
    try:
        return decode_history_list(value)
    except ProtocolError as exc:
        raise StartTransportError from exc


def _exact_object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise StartTransportError
    return value
