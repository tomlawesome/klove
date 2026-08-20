from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

KLOVE = "http://klove:8080"
PROXY_CONTROL = "http://moonraker-proxy:9126"
PRINTER_ID = "simulated-printer"
TOKEN = Path("/run/klove-secrets/klove-token").read_text(encoding="utf-8").strip()
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
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.load(error)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _headers(*, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _snapshot() -> dict[str, Any]:
    status, document = _request_json(f"{KLOVE}/v1/printers/{PRINTER_ID}", headers=_headers())
    if status != 200 or not isinstance(document, dict):
        raise RuntimeError("Klove snapshot request failed")
    return document


def _wait_reconnected(old_token: str, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    last_snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_snapshot = _snapshot()
        current_token = last_snapshot.get("state_token")
        if (
            last_snapshot.get("phase") == "idle"
            and last_snapshot.get("reason") == "observed"
            and isinstance(current_token, str)
            and current_token != old_token
        ):
            return current_token
        time.sleep(0.1)
    raise RuntimeError(
        "Klove did not reconnect with a new idle state token: "
        f"phase={last_snapshot.get('phase')!r} "
        f"reason={last_snapshot.get('reason')!r} "
        f"connected={last_snapshot.get('connected')!r} "
        f"token_changed={last_snapshot.get('state_token') != old_token}"
    )


def _cancel_count() -> int:
    status, document = _request_json(f"{PROXY_CONTROL}/stats")
    _require(status == 200, "proxy stats request failed")
    value = document["counts"]["printer.print.cancel"]
    if not isinstance(value, int):
        raise RuntimeError("proxy returned an invalid dispatch count")
    return value


def _save_state_token(state_token: str) -> None:
    temporary = STATE_TOKEN_PATH.with_suffix(".new")
    temporary.write_text(f"{state_token}\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(STATE_TOKEN_PATH)


def main() -> int:
    expected_code = os.environ.get("KLOVE_SIM_EXPECTED_CODE", "")
    expected = {
        "outcome_unknown": (
            202,
            {"operation": "cancel", "status": "outcome_unknown", "code": "outcome_unknown"},
        ),
        "state_token_mismatch": (
            409,
            {"operation": "cancel", "status": "denied", "code": "state_token_mismatch"},
        ),
    }.get(expected_code)
    if expected is None:
        raise RuntimeError("KLOVE_SIM_EXPECTED_CODE is invalid")

    old_token = STATE_TOKEN_PATH.read_text(encoding="utf-8").strip()
    current_token = _wait_reconnected(old_token)
    before = _cancel_count()
    status, document = _request_json(
        f"{KLOVE}/v1/printers/{PRINTER_ID}/commands/cancel",
        method="POST",
        body={"state_token": old_token},
        headers=_headers(key=str(uuid.uuid4())),
    )
    expected_status, expected_document = expected
    _require(
        status == expected_status,
        f"old state token returned unexpected status {status}",
    )
    _require(
        document == expected_document,
        "old state token returned an unexpected result",
    )
    _require(_cancel_count() == before, "old state token reached Moonraker after reconnect")
    _save_state_token(current_token)
    print("moonraker-sim reconnect contract passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
