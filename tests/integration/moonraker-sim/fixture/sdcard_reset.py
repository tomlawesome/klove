"""Exact simulator-only virtual-SD reset between independent dispatch scenarios."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, NoReturn

import aiohttp

_RESET_URL = "http://moonraker-proxy:7125/printer/gcode/script"
_RESET_SCRIPT = "SDCARD_RESET_FILE"
_MAX_RESPONSE_BYTES = 1024


class _DuplicateObjectMember(Exception):
    pass


def _reject(category: str) -> NoReturn:
    raise RuntimeError(f"fixture virtual-SD reset response was invalid: {category}")


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateObjectMember
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError


def _require_exact_response(
    *,
    status: int,
    content_types: list[str],
    content_type: str,
    charset: str | None,
    body: bytes,
) -> None:
    """Accept only Moonraker's exact bounded ``ok`` acknowledgement."""
    if status != 200:
        _reject("status")
    if (
        len(content_types) != 1
        or content_type != "application/json"
        or (charset is not None and charset.casefold() != "utf-8")
    ):
        _reject("content_type")
    if len(body) > _MAX_RESPONSE_BYTES:
        _reject("size")
    try:
        document = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except _DuplicateObjectMember:
        _reject("duplicate")
    except (UnicodeError, json.JSONDecodeError, ValueError):
        _reject("json")
    if document != {"result": "ok"}:
        _reject("shape")


async def reset_virtual_sd_after_completed_print(
    session: aiohttp.ClientSession,
    api_key: str,
) -> None:
    """Send the one fixed test-only reset; never accept caller-controlled G-code."""
    try:
        async with session.post(
            _RESET_URL,
            params={"script": _RESET_SCRIPT},
            headers={"X-Api-Key": api_key},
            allow_redirects=False,
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(_MAX_RESPONSE_BYTES):
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    _reject("size")
            _require_exact_response(
                status=response.status,
                content_types=response.headers.getall("Content-Type", []),
                content_type=response.content_type,
                charset=response.charset,
                body=bytes(body),
            )
    except (aiohttp.ClientError, TimeoutError, OSError) as error:
        raise RuntimeError("fixture virtual-SD reset request failed") from error
