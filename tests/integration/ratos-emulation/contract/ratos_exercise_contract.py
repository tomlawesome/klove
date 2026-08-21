from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_shared_contract() -> ModuleType:
    installed = Path(__file__).with_name("exercise_contract.py")
    source = (
        Path(__file__).resolve().parents[2] / "moonraker-sim" / "fixture" / "exercise_contract.py"
    )
    contract_path = installed if installed.is_file() else source
    specification = importlib.util.spec_from_file_location("_klove_shared_contract", contract_path)
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load the shared Moonraker contract fixture")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(specification.name, None)
    return module


contract = _load_shared_contract()


def _history_identity(expected_status: str) -> tuple[str, float]:
    status, document = contract._request_json(
        f"{contract.MOONRAKER}/server/history/list?limit=1&start=0&order=desc",
        headers=contract._moonraker_headers(),
    )
    contract._require(
        status == 200 and isinstance(document, dict), "Moonraker history request failed"
    )
    try:
        result = document["result"]
        count = result["count"]
        jobs = result["jobs"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("Moonraker returned malformed history evidence") from error
    contract._require(
        type(count) is int and count == 1 and isinstance(jobs, list) and len(jobs) == 1,
        "Moonraker did not return one current history job",
    )
    job: Any = jobs[0]
    contract._require(isinstance(job, dict), "Moonraker returned a malformed history job")
    job_id = job.get("job_id")
    start_time = job.get("start_time")
    contract._require(
        isinstance(job_id, str)
        and 6 <= len(job_id) <= 16
        and all(character in "0123456789ABCDEF" for character in job_id)
        and isinstance(start_time, (int, float))
        and not isinstance(start_time, bool)
        and start_time >= 0
        and job.get("filename") == "contract.gcode"
        and job.get("status") == expected_status,
        "Moonraker returned an unexpected immutable history identity",
    )
    return job_id, float(start_time)


def main() -> int:  # noqa: PLR0915 -- linear safety contract with ordered evidence.
    status, _document = contract._request_json(
        f"{contract.KLOVE}/v1/printers/{contract.PRINTER_ID}",
        headers=contract._klove_headers(token=uuid.uuid4().hex),
    )
    contract._require(status == 401, "invalid bearer token was not rejected")

    status, document = contract._request_json(
        f"{contract.MOONRAKER_PROXY}/printer/objects/query?print_stats",
        headers=contract._moonraker_headers(api_key="0" * 32, direct=False),
    )
    if contract.MOONRAKER_AUTH_EXPECTATION == "rejected":
        contract._require(status == 401, "invalid Moonraker API key was not rejected")
    else:
        contract._require(status == 200, "stock trusted Moonraker transport was not accepted")
        try:
            trusted_phase = document["result"]["status"]["print_stats"]["state"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("trusted Moonraker transport returned malformed state") from error
        contract._require(
            trusted_phase == "standby", "trusted Moonraker transport returned wrong state"
        )

    contract._start_test_print()
    printing = contract._wait_klove_phase("printing")
    contract._require(
        printing["status"]["print_stats"]["filename"] == "contract.gcode",
        "Klove observed the wrong print job",
    )
    started_history = _history_identity("in_progress")

    pause_before = contract._proxy_count("pause")
    initial_token, pause_key, pause_result = contract._control_current("pause", "printing")
    contract._expect_result(
        pause_result, status_code=200, operation="pause", status="confirmed", code="confirmed"
    )
    contract._require(
        contract._proxy_count("pause") == pause_before + 1,
        "pause was not dispatched exactly once",
    )
    contract._wait_klove_phase("paused")
    contract._expect_result(
        contract._control("pause", initial_token, pause_key),
        status_code=200,
        operation="pause",
        status="confirmed",
        code="confirmed",
    )
    contract._require(
        contract._proxy_count("pause") == pause_before + 1,
        "duplicate pause was redispatched",
    )
    cancel_before = contract._proxy_count("cancel")
    contract._expect_result(
        contract._control("cancel", initial_token, str(uuid.uuid4())),
        status_code=409,
        operation="cancel",
        status="denied",
        code="state_token_mismatch",
    )
    contract._require(
        contract._proxy_count("cancel") == cancel_before,
        "stale cancel reached Moonraker",
    )

    resume_before = contract._proxy_count("resume")
    _resume_token, _resume_key, resume_result = contract._control_current("resume", "paused")
    contract._expect_result(
        resume_result, status_code=200, operation="resume", status="confirmed", code="confirmed"
    )
    contract._require(
        contract._proxy_count("resume") == resume_before + 1,
        "resume was not dispatched exactly once",
    )
    contract._wait_klove_phase("printing")

    before = contract._proxy_count("pause")
    arm_status, _arm_document = contract._request_json(
        f"{contract.PROXY_CONTROL}/arm/pause", method="POST"
    )
    contract._require(arm_status == 200, "fault proxy could not arm")
    ambiguous_token, ambiguous_key, ambiguous_result = contract._control_current(
        "pause", "printing"
    )
    contract._expect_result(
        ambiguous_result,
        status_code=202,
        operation="pause",
        status="outcome_unknown",
        code="outcome_unknown",
    )
    contract._wait_moonraker_phase("paused")
    faulted_pause_history = _history_identity("in_progress")
    contract._require(
        faulted_pause_history == started_history,
        "faulted pause did not retain the original Moonraker history identity",
    )
    contract._expect_result(
        contract._control("pause", ambiguous_token, ambiguous_key),
        status_code=202,
        operation="pause",
        status="outcome_unknown",
        code="outcome_unknown",
    )
    contract._expect_result(
        contract._control("pause", ambiguous_token, str(uuid.uuid4())),
        status_code=202,
        operation="pause",
        status="outcome_unknown",
        code="outcome_unknown",
    )
    contract._require(
        contract._proxy_count("pause") == before + 1,
        "ambiguous action was dispatched more than once",
    )

    paused = contract._wait_klove_phase("paused")
    paused_token = paused.get("state_token")
    contract._require(
        isinstance(paused_token, str) and paused_token != ambiguous_token,
        "Klove did not replace the outcome-unknown token after direct pause evidence",
    )
    cancel_before = contract._proxy_count("cancel")
    _cancel_token, _cancel_key, cancel_result = contract._control_current("cancel", "paused")
    contract._expect_result(
        cancel_result, status_code=200, operation="cancel", status="confirmed", code="confirmed"
    )
    contract._require(
        contract._proxy_count("cancel") == cancel_before + 1,
        "cancel was not dispatched exactly once",
    )
    contract._wait_klove_phase("cancelled")
    cancelled_history = _history_identity("cancelled")
    contract._require(
        cancelled_history == started_history,
        "terminal cancel did not retain the original Moonraker history identity",
    )
    contract._save_state_token(ambiguous_token)
    print(
        json.dumps(
            {
                "history": {
                    "job_id": started_history[0],
                    "phases": {
                        "cancelled": "cancelled",
                        "faulted_pause": "in_progress",
                        "started": "in_progress",
                    },
                    "start_time": started_history[1],
                }
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
