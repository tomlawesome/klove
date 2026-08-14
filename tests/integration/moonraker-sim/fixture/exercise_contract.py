from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def _environment_url(name: str, default: str) -> str:
    raw = os.environ.get(name)
    value = default if raw is None or raw == "" else raw
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not parsed.hostname.isascii()
        or any(character.isspace() or ord(character) < 32 for character in parsed.hostname)
    ):
        raise ValueError(f"{name} must be an uncredentialed HTTP(S) origin")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{name} contains an invalid port")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _environment_host_header(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    if (
        len(raw) > 255
        or not raw.isascii()
        or any(character.isspace() or not 33 <= ord(character) <= 126 for character in raw)
    ):
        raise ValueError(f"{name} contains an invalid HTTP Host value")
    parsed = urllib.parse.urlsplit(f"http://{raw}")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} contains an invalid HTTP Host value") from error
    if (
        parsed.netloc != raw
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(f"{name} contains an invalid HTTP Host value")
    return raw


def _environment_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = os.environ.get(name)
    value = default if raw is None or raw == "" else raw
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


KLOVE = _environment_url("KLOVE_TEST_KLOVE_URL", "http://klove:8080")
MOONRAKER = _environment_url("KLOVE_TEST_MOONRAKER_URL", "http://printer-host:7125")
MOONRAKER_PROXY = _environment_url("KLOVE_TEST_MOONRAKER_PROXY_URL", "http://moonraker-proxy:7125")
PROXY_CONTROL = _environment_url("KLOVE_TEST_PROXY_CONTROL_URL", "http://moonraker-proxy:9126")
MOONRAKER_HOST_HEADER = _environment_host_header("KLOVE_TEST_MOONRAKER_HOST_HEADER")
MOONRAKER_AUTH_EXPECTATION = _environment_choice(
    "KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION", "rejected", {"rejected", "trusted"}
)
PRINTER_ID = "simulated-printer"
TOKEN_PATH = Path("/run/klove-secrets/klove-token")
MOONRAKER_KEY_PATH = Path("/run/klove-secrets/moonraker-api-key")
STATE_TOKEN_PATH = Path("/run/test-state/state-token")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 -- caller uses fixed internal URLs.
        url, data=encoded, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- fixed URLs.
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.load(error)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("contract secret file is empty")
    return value


def _klove_headers(*, key: str | None = None, token: str | None = None) -> dict[str, str]:
    actual_token = _read_secret(TOKEN_PATH) if token is None else token
    headers = {
        "Authorization": f"Bearer {actual_token}",
        "Content-Type": "application/json",
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _moonraker_headers(*, api_key: str | None = None, direct: bool = True) -> dict[str, str]:
    actual_key = _read_secret(MOONRAKER_KEY_PATH) if api_key is None else api_key
    headers = {"Content-Type": "application/json", "X-Api-Key": actual_key}
    if direct and MOONRAKER_HOST_HEADER is not None:
        headers["Host"] = MOONRAKER_HOST_HEADER
    return headers


def _snapshot() -> dict[str, Any]:
    status, document = _request_json(f"{KLOVE}/v1/printers/{PRINTER_ID}", headers=_klove_headers())
    if status != 200 or not isinstance(document, dict):
        raise RuntimeError("Klove snapshot request failed")
    return document


def _wait_klove_phase(phase: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _snapshot()
        if snapshot.get("phase") == phase and snapshot.get("reason") == "observed":
            return snapshot
        time.sleep(0.1)
    raise RuntimeError(f"Klove did not observe phase {phase}")


def _moonraker_phase() -> str:
    _status, document = _request_json(
        f"{MOONRAKER}/printer/objects/query?print_stats", headers=_moonraker_headers()
    )
    try:
        phase = document["result"]["status"]["print_stats"]["state"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("Moonraker returned malformed print state") from error
    if not isinstance(phase, str):
        raise RuntimeError("Moonraker returned a non-string print state")
    return phase


def _wait_moonraker_phase(phase: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _moonraker_phase() == phase:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Moonraker did not enter phase {phase}")


def _control(operation: str, state_token: str, key: str) -> tuple[int, Any]:
    return _request_json(
        f"{KLOVE}/v1/printers/{PRINTER_ID}/commands/{operation}",
        method="POST",
        body={"state_token": state_token},
        headers=_klove_headers(key=key),
    )


def _control_current(
    operation: str, expected_phase: str, timeout: float = 10
) -> tuple[str, str, tuple[int, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _snapshot()
        if snapshot.get("phase") != expected_phase:
            time.sleep(0.02)
            continue
        state_token = snapshot.get("state_token")
        if not isinstance(state_token, str):
            raise RuntimeError("Klove omitted the state token")
        key = str(uuid.uuid4())
        response = _control(operation, state_token, key)
        status_code, document = response
        if (
            status_code == 409
            and isinstance(document, dict)
            and document.get("code") in {"job_identity_unavailable", "state_token_mismatch"}
        ):
            time.sleep(0.1)
            continue
        return state_token, key, response
    raise RuntimeError(f"could not submit current {operation} intent")


def _expect_result(
    response: tuple[int, Any], *, status_code: int, operation: str, status: str, code: str
) -> None:
    actual_status, document = response
    _require(
        actual_status == status_code,
        f"unexpected Klove status for {operation}: {actual_status} {document!r}",
    )
    _require(
        document == {"operation": operation, "status": status, "code": code},
        f"unexpected Klove result for {operation}",
    )


def _start_test_print() -> None:
    filename = urllib.parse.quote("contract.gcode", safe="")
    status, _document = _request_json(
        f"{MOONRAKER}/printer/print/start?filename={filename}",
        method="POST",
        headers=_moonraker_headers(),
    )
    _require(status == 200, "test-only print preparation failed")
    _wait_moonraker_phase("printing")


def _proxy_count(operation: str) -> int:
    status, document = _request_json(f"{PROXY_CONTROL}/stats")
    _require(status == 200, "proxy stats request failed")
    value = document["counts"][f"printer.print.{operation}"]
    if not isinstance(value, int):
        raise RuntimeError("proxy returned an invalid dispatch count")
    return value


def _save_state_token(state_token: str) -> None:
    temporary = STATE_TOKEN_PATH.with_suffix(".new")
    temporary.write_text(f"{state_token}\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(STATE_TOKEN_PATH)


def main() -> int:
    status, _document = _request_json(
        f"{KLOVE}/v1/printers/{PRINTER_ID}",
        headers=_klove_headers(token=uuid.uuid4().hex),
    )
    _require(status == 401, "invalid bearer token was not rejected")

    status, document = _request_json(
        f"{MOONRAKER_PROXY}/printer/objects/query?print_stats",
        headers=_moonraker_headers(api_key="0" * 32, direct=False),
    )
    if MOONRAKER_AUTH_EXPECTATION == "rejected":
        _require(status == 401, "invalid Moonraker API key was not rejected")
    else:
        _require(status == 200, "stock trusted Moonraker transport was not accepted")
        try:
            trusted_phase = document["result"]["status"]["print_stats"]["state"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("trusted Moonraker transport returned malformed state") from error
        _require(trusted_phase == "standby", "trusted Moonraker transport returned wrong state")

    _start_test_print()
    printing = _wait_klove_phase("printing")
    _require(
        printing["status"]["print_stats"]["filename"] == "contract.gcode",
        "Klove observed the wrong print job",
    )

    pause_before = _proxy_count("pause")
    initial_token, pause_key, pause_result = _control_current("pause", "printing")
    _expect_result(
        pause_result, status_code=200, operation="pause", status="confirmed", code="confirmed"
    )
    _require(_proxy_count("pause") == pause_before + 1, "pause was not dispatched exactly once")
    _wait_klove_phase("paused")
    _expect_result(
        _control("pause", initial_token, pause_key),
        status_code=200,
        operation="pause",
        status="confirmed",
        code="confirmed",
    )
    _require(_proxy_count("pause") == pause_before + 1, "duplicate pause was redispatched")
    cancel_before = _proxy_count("cancel")
    _expect_result(
        _control("cancel", initial_token, str(uuid.uuid4())),
        status_code=409,
        operation="cancel",
        status="denied",
        code="state_token_mismatch",
    )
    _require(_proxy_count("cancel") == cancel_before, "stale cancel reached Moonraker")

    _wait_klove_phase("paused")
    resume_before = _proxy_count("resume")
    _resume_token, _resume_key, resume_result = _control_current("resume", "paused")
    _expect_result(
        resume_result,
        status_code=200,
        operation="resume",
        status="confirmed",
        code="confirmed",
    )
    _require(
        _proxy_count("resume") == resume_before + 1,
        "resume was not dispatched exactly once",
    )
    _wait_klove_phase("printing")
    cancel_before = _proxy_count("cancel")
    _cancel_token, _cancel_key, cancel_result = _control_current("cancel", "printing")
    _expect_result(
        cancel_result,
        status_code=200,
        operation="cancel",
        status="confirmed",
        code="confirmed",
    )
    _require(
        _proxy_count("cancel") == cancel_before + 1,
        "cancel was not dispatched exactly once",
    )
    _wait_klove_phase("cancelled")

    _start_test_print()
    _wait_klove_phase("printing")
    before = _proxy_count("pause")
    arm_status, _arm_document = _request_json(f"{PROXY_CONTROL}/arm/pause", method="POST")
    _require(arm_status == 200, "fault proxy could not arm")
    ambiguous_token, ambiguous_key, ambiguous_result = _control_current("pause", "printing")
    _expect_result(
        ambiguous_result,
        status_code=202,
        operation="pause",
        status="outcome_unknown",
        code="outcome_unknown",
    )
    _wait_moonraker_phase("paused")
    _expect_result(
        _control("pause", ambiguous_token, ambiguous_key),
        status_code=202,
        operation="pause",
        status="outcome_unknown",
        code="outcome_unknown",
    )
    _expect_result(
        _control("pause", ambiguous_token, str(uuid.uuid4())),
        status_code=202,
        operation="pause",
        status="outcome_unknown",
        code="outcome_unknown",
    )
    _require(_proxy_count("pause") == before + 1, "ambiguous action was dispatched more than once")
    _save_state_token(ambiguous_token)
    print("moonraker-sim contract passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
