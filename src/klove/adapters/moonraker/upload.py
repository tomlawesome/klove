"""Bounded Moonraker file upload, metadata, and remote-digest transport."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO
from urllib.parse import quote

import aiohttp
from pydantic import ValidationError

from klove.config import PrinterConfig
from klove.domain.artifacts import Sha256Digest
from klove.domain.upload import (
    MOONRAKER_GCODE_PATH_PATTERN,
    MoonrakerGcodeMetadata,
    MoonrakerUploadReceipt,
    RemoteFileDigest,
)
from klove.errors import UploadTransportError

_MAX_JSON_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PATH = re.compile(MOONRAKER_GCODE_PATH_PATTERN)


class MoonrakerUploadTransport:
    """Write and independently verify one exact file through Moonraker HTTP."""

    def __init__(
        self,
        config: PrinterConfig,
        api_key: str,
        session: aiohttp.ClientSession,
        *,
        request_timeout_seconds: float,
    ) -> None:
        self._base_url = config.endpoint.rstrip("/")
        self._api_key = api_key
        self._session = session
        self._verify_tls = config.verify_tls
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)

    async def upload(
        self,
        path: str,
        content: BinaryIO,
        sha256: Sha256Digest,
        size_bytes: int,
    ) -> MoonrakerUploadReceipt:
        """Upload exactly once with Moonraker checksum verification and no start."""
        _require_path(path)
        filename = path.removeprefix("klove/")
        form = aiohttp.FormData(quote_fields=True)
        form.add_field("root", "gcodes")
        form.add_field("path", "klove")
        form.add_field("checksum", sha256.removeprefix("sha256:"))
        form.add_field("print", "false")
        form.add_field(
            "file",
            content,
            filename=filename,
            content_type="application/octet-stream",
        )
        try:
            async with self._session.post(
                f"{self._base_url}/server/files/upload",
                data=form,
                headers=self._headers,
                allow_redirects=False,
                ssl=self._verify_tls,
                timeout=self._timeout,
            ) as response:
                document = await _json_document(response, expected_status=201)
                locations = response.headers.getall("Location", [])
                if locations != [f"/server/files/gcodes/{path}"]:
                    raise UploadTransportError
            receipt = _decode_upload(document)
        except (
            aiohttp.ClientError,
            TimeoutError,
            OSError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise UploadTransportError from exc
        if receipt.path != path or receipt.size_bytes != size_bytes:
            raise UploadTransportError
        return receipt

    async def metadata(self, path: str) -> MoonrakerGcodeMetadata | None:
        """Read strict selected metadata, returning ``None`` only while absent."""
        _require_path(path)
        try:
            async with self._session.get(
                f"{self._base_url}/server/files/metadata",
                params={"filename": path},
                headers=self._headers,
                allow_redirects=False,
                ssl=self._verify_tls,
                timeout=self._timeout,
            ) as response:
                if response.status == 404:
                    return None
                document = await _json_document(response, expected_status=200)
            return _decode_metadata(document, path)
        except (
            aiohttp.ClientError,
            TimeoutError,
            InvalidOperation,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise UploadTransportError from exc

    async def download(self, path: str, expected_size: int) -> RemoteFileDigest:
        """Stream the exact remote file back through a bounded SHA-256 digest."""
        _require_path(path)
        digest = hashlib.sha256()
        size_bytes = 0
        encoded_path = quote(path, safe="/")
        try:
            async with self._session.get(
                f"{self._base_url}/server/files/gcodes/{encoded_path}",
                headers=self._headers,
                allow_redirects=False,
                ssl=self._verify_tls,
                timeout=self._timeout,
            ) as response:
                if response.status != 200:
                    raise UploadTransportError
                lengths = response.headers.getall("Content-Length", [])
                if lengths != [str(expected_size)]:
                    raise UploadTransportError
                async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
                    size_bytes += len(chunk)
                    if size_bytes > expected_size:
                        raise UploadTransportError
                    digest.update(chunk)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
            raise UploadTransportError from exc
        if size_bytes != expected_size:
            raise UploadTransportError
        return RemoteFileDigest(
            size_bytes=size_bytes,
            sha256=f"sha256:{digest.hexdigest()}",
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}


async def _json_document(
    response: aiohttp.ClientResponse,
    *,
    expected_status: int,
) -> object:
    if response.status != expected_status:
        raise UploadTransportError
    content_types = response.headers.getall("Content-Type", [])
    if len(content_types) != 1 or response.content_type != "application/json":
        raise UploadTransportError
    if response.charset is not None and response.charset.casefold() != "utf-8":
        raise UploadTransportError
    body = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        body.extend(chunk)
        if len(body) > _MAX_JSON_BYTES:
            raise UploadTransportError
    try:
        return json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UploadTransportError from exc


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object member", key, 0)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise json.JSONDecodeError("non-finite number", value, 0)


def _decode_upload(document: object) -> MoonrakerUploadReceipt:
    result = _exact_object(document, {"item", "print_started", "print_queued", "action"})
    item = _exact_object(result["item"], {"root", "path", "modified", "size", "permissions"})
    if (
        result["print_started"] is not False
        or result["print_queued"] is not False
        or result["action"] != "create_file"
        or item["root"] != "gcodes"
        or item["permissions"] != "rw"
        or not isinstance(item["path"], str)
    ):
        raise UploadTransportError
    return MoonrakerUploadReceipt(
        path=item["path"],
        size_bytes=_integer(item["size"]),
        modified=_decimal(item["modified"]),
    )


def _decode_metadata(document: object, path: str) -> MoonrakerGcodeMetadata:
    envelope = _exact_object(document, {"result"})
    result = envelope["result"]
    required = {
        "filename",
        "size",
        "modified",
        "uuid",
        "file_processors",
        "nozzle_diameter",
        "gcode_start_byte",
        "gcode_end_byte",
    }
    if not isinstance(result, dict) or not required.issubset(result):
        raise UploadTransportError
    if result["filename"] != path or not isinstance(result["uuid"], str):
        raise UploadTransportError
    processors = result["file_processors"]
    if not isinstance(processors, list) or not all(type(item) is str for item in processors):
        raise UploadTransportError
    if result.get("job_id") is not None or result.get("print_start_time") is not None:
        raise UploadTransportError
    nozzle_micrometres = _decimal(result["nozzle_diameter"]) * 1000
    if nozzle_micrometres != nozzle_micrometres.to_integral_value():
        raise UploadTransportError
    return MoonrakerGcodeMetadata(
        path=path,
        size_bytes=_integer(result["size"]),
        modified=_decimal(result["modified"]),
        metadata_uuid=result["uuid"],
        file_processors=tuple(processors),
        nozzle_diameter_micrometres=int(nozzle_micrometres),
        gcode_start_byte=_integer(result["gcode_start_byte"]),
        gcode_end_byte=_integer(result["gcode_end_byte"]),
        job_id=None,
        print_start_time=None,
    )


def _exact_object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise UploadTransportError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise UploadTransportError
    return value


def _decimal(value: object) -> Decimal:
    if type(value) is int:
        return Decimal(value)
    if type(value) is not Decimal or not value.is_finite():
        raise UploadTransportError
    return value


def _require_path(path: str) -> None:
    if _PATH.fullmatch(path) is None:
        raise UploadTransportError
