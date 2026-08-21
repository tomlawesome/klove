from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = (
    ROOT / "tests" / "integration" / "ratos-emulation" / "contract" / "ratos_exercise_contract.py"
)
TOOL = ROOT / "tests" / "integration" / "ratos-emulation" / "tool.py"


def _load_runner() -> ModuleType:
    module_name = f"_klove_ratos_contract_{uuid.uuid4().hex}"
    specification = importlib.util.spec_from_file_location(module_name, RUNNER)
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load RatOS contract runner")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _load_tool() -> ModuleType:
    module_name = f"_klove_ratos_tool_{uuid.uuid4().hex}"
    specification = importlib.util.spec_from_file_location(module_name, TOOL)
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load RatOS tool")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def test_ratos_authorization_replacement_removes_trusted_loopback() -> None:
    tool = _load_tool()

    rewritten = tool._untrust_moonraker_clients(
        b"[server]\nport: 7125\n\n[authorization]\ntrusted_clients:\n  127.0.0.1\n\n[history]\n"
    )
    assert rewritten == (
        b"[server]\nport: 7125\n\n[authorization]\ntrusted_clients:\n  192.0.2.0/24\n\n[history]\n"
    )


def test_ratos_contract_installs_controlled_printer_before_first_ready_wait() -> None:
    tool = _load_tool()
    preparation = inspect.getsource(tool.contract_prepare)

    assert (
        preparation.index("api_key = _moonraker_api_key()")
        < preparation.index('filename="printer.cfg"')
        < preparation.index('failure_stage="after_printer_restart"')
        < preparation.index("moonraker_config = _replace_moonraker_configuration(api_key)")
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        ("Config error", "config-parse"),
        ("MCU connect socket", "mcu-connect-socket"),
        ("MCU protocol mismatch", "mcu-protocol"),
        ("restart requested", "restart-pending"),
        ("private unexpected text", "unknown"),
    ),
)
def test_ratos_klippy_message_classification_is_closed(message: str, expected: str) -> None:
    assert _load_tool()._classify_klippy_message(message) == expected


def test_ratos_readiness_record_redacts_hostile_remote_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = _load_tool()
    hostile_state = "private-state-value"
    hostile_message = "private klippy diagnostic"
    monkeypatch.setattr(tool, "READINESS_FAILURE", tmp_path / "failure.json")
    monkeypatch.setattr(tool, "_socket_evidence", lambda: {"kind": "absent"})
    printer = {"status": "ok", **tool._message_evidence(hostile_message)}
    tool._write_readiness_failure(
        stage="after_printer_restart",
        elapsed_seconds=31,
        cycle={
            "restart_acknowledged": True,
            "disconnect_observed": False,
            "reconnect_observed": False,
        },
        config=b"fixture",
        server={"status": "ok", "klippy_state": tool._closed_state(hostile_state, {"ready"})},
        printer=printer,
    )
    serialized = (tmp_path / "failure.json").read_text(encoding="utf-8")
    assert hostile_state not in serialized
    assert hostile_message not in serialized
    record = json.loads(serialized)
    assert record["server"]["klippy_state"] == "unknown"
    assert record["printer"]["message"] == "unknown"
    assert record["printer"]["message_bytes"] == len(hostile_message.encode())


def test_ratos_readiness_transport_failure_retains_server_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    clocks = iter((0.0, 0.0, 0.0, 1.0, 1.0))
    captured: dict[str, object] = {}
    monkeypatch.setattr(tool.time, "monotonic", lambda: next(clocks))
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        tool,
        "_http_json",
        lambda *_args, **_kwargs: {"result": {"klippy_connected": True, "klippy_state": "ready"}},
    )
    monkeypatch.setattr(
        tool, "_http_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError())
    )
    monkeypatch.setattr(tool, "_socket_evidence", lambda: {"kind": "absent"})
    monkeypatch.setattr(tool, "READINESS_FAILURE", Path("/unused"))
    monkeypatch.setattr(tool, "_write_readiness_failure", lambda **kwargs: captured.update(kwargs))

    with pytest.raises(RuntimeError, match="did not become ready"):
        tool._wait_printer_ready(1, failure_stage="after_printer_restart", config=b"fixture")

    assert captured["server"] == {"status": "ok", "klippy_state": "ready", "klippy_connected": True}
    assert captured["printer"] == {
        "status": "unavailable",
        "http_status": None,
        "message": "unknown",
        "message_sha256": None,
        "message_bytes": 0,
    }


def test_ratos_readiness_404_error_envelope_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = _load_tool()
    message = "MCU socket connection failed"
    clocks = iter((0.0, 0.0, 0.0, 1.0, 1.0))
    monkeypatch.setattr(tool.time, "monotonic", lambda: next(clocks))
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(tool, "READINESS_FAILURE", tmp_path / "failure.json")
    monkeypatch.setattr(tool, "_socket_evidence", lambda: {"kind": "absent"})
    monkeypatch.setattr(
        tool,
        "_http_json",
        lambda *_args, **_kwargs: {"result": {"klippy_connected": True, "klippy_state": "startup"}},
    )
    monkeypatch.setattr(
        tool,
        "_http_request",
        lambda *_args, **_kwargs: (
            404,
            json.dumps({"error": {"code": 404, "message": message}}).encode(),
        ),
    )
    with pytest.raises(RuntimeError):
        tool._wait_printer_ready(1, failure_stage="after_printer_restart", config=b"fixture")
    serialized = (tmp_path / "failure.json").read_text(encoding="utf-8")
    assert message not in serialized
    printer = json.loads(serialized)["printer"]
    assert printer["http_status"] == 404
    assert printer["message"] == "mcu-connect-socket"
    assert printer["message_bytes"] == len(message.encode())


@pytest.mark.parametrize(
    "configuration",
    (
        b"[authorization]\n",
        b"[authorization]\ntrusted_clients:\n[authorization]\ntrusted_clients:\n",
        b"[authorization]\ntrusted_clients:\ntrusted_clients:\n",
    ),
)
def test_ratos_authorization_replacement_rejects_ambiguous_configuration(
    configuration: bytes,
) -> None:
    tool = _load_tool()

    with pytest.raises(RuntimeError):
        tool._untrust_moonraker_clients(configuration)


@pytest.mark.parametrize("action", ("create_file", "modify_file"))
def test_ratos_configuration_upload_accepts_only_exact_file_actions(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    tool = _load_tool()
    original = b"[authorization]\ntrusted_clients:\n  127.0.0.1\n"
    replacement = b"[authorization]\ntrusted_clients:\n  192.0.2.0/24\n"
    uploads: list[dict[str, object]] = []

    monkeypatch.setattr(tool, "_download_contract_file", lambda *_args, **_kwargs: original)

    def http_json(_path: str, **kwargs: object) -> dict[str, object]:
        uploads.append(kwargs)
        if len(uploads) == 1:
            return {
                "item": {
                    "root": "config",
                    "path": "moonraker.conf",
                    "modified": 1.0,
                    "size": len(replacement),
                    "permissions": "rw",
                },
                "action": action,
            }
        return {"result": "ok"}

    monkeypatch.setattr(tool, "_http_json", http_json)
    verified: list[bytes] = []
    monkeypatch.setattr(
        tool,
        "_verify_remote_contract_file",
        lambda *_args, **kwargs: verified.append(kwargs["expected"]),
    )

    assert tool._replace_moonraker_configuration("a" * 32) == replacement
    assert verified == [replacement]
    assert len(uploads) == 2


def test_ratos_configuration_upload_rejects_unexpected_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    original = b"[authorization]\ntrusted_clients:\n  127.0.0.1\n"
    monkeypatch.setattr(tool, "_download_contract_file", lambda *_args, **_kwargs: original)
    monkeypatch.setattr(
        tool,
        "_http_json",
        lambda *_args, **_kwargs: {
            "item": {
                "root": "config",
                "path": "moonraker.conf",
                "modified": 1.0,
                "size": 51,
                "permissions": "rw",
            },
            "action": "delete_file",
        },
    )

    with pytest.raises(RuntimeError, match="did not replace"):
        tool._replace_moonraker_configuration("a" * 32)


def test_ratos_contract_evidence_rejects_history_phase_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    runner_input = (
        b'{"history":{"job_id":"ABC123","phases":{"started":"in_progress",'
        b'"faulted_pause":"in_progress","cancelled":"cancelled"},"start_time":1.0}}'
    )

    class Input:
        class Buffer:
            def read(self, _limit: int) -> bytes:
                return runner_input

        buffer = Buffer()

    monkeypatch.setattr(tool.sys, "stdin", Input())
    assert tool._runner_history_evidence() == ("ABC123", 1.0)

    substituted = runner_input.replace(
        b'"faulted_pause":"in_progress"', b'"faulted_pause":"cancelled"'
    )
    monkeypatch.setattr(Input.Buffer, "read", lambda _self, _limit: substituted)
    with pytest.raises(RuntimeError, match="phases"):
        tool._runner_history_evidence()


def test_ratos_contract_keeps_runner_history_input_attached_to_docker_exec() -> None:
    contract_script = (ROOT / "scripts" / "ratos-emulation-contract.sh").read_text(encoding="utf-8")

    assert 'docker exec --interactive "$ratos_container"' in contract_script
    assert '< "$contract_output" \\' in contract_script


def test_ratos_contract_keeps_lost_response_and_final_cancel_on_one_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    contract = runner.contract
    starts = 0
    saved: list[str] = []
    counts = {
        "pause": iter((0, 1, 1, 1, 2)),
        "resume": iter((0, 1)),
        "cancel": iter((0, 0, 0, 1)),
    }
    controls = iter(
        (
            (
                "initial",
                "pause",
                (200, {"operation": "pause", "status": "confirmed", "code": "confirmed"}),
            ),
            (
                "resume",
                "resume",
                (200, {"operation": "resume", "status": "confirmed", "code": "confirmed"}),
            ),
            (
                "ambiguous",
                "pause",
                (
                    202,
                    {"operation": "pause", "status": "outcome_unknown", "code": "outcome_unknown"},
                ),
            ),
            (
                "cancel",
                "cancel",
                (200, {"operation": "cancel", "status": "confirmed", "code": "confirmed"}),
            ),
        )
    )

    def request_json(url: str, **_kwargs: object) -> tuple[int, object]:
        if url.endswith(f"/printers/{contract.PRINTER_ID}"):
            return 401, {}
        if "printer/objects/query?print_stats" in url:
            return 401, {}
        if url.endswith("/arm/pause"):
            return 200, {"status": "armed"}
        raise AssertionError(f"unexpected request: {url}")

    def start_test_print() -> None:
        nonlocal starts
        starts += 1

    def wait_phase(phase: str) -> dict[str, object]:
        if phase == "printing":
            return {"status": {"print_stats": {"filename": "contract.gcode"}}}
        if phase == "paused":
            return {"state_token": "p" * 64}
        return {"state_token": "c" * 64}

    def control_current(operation: str, _phase: str) -> tuple[str, str, tuple[int, dict[str, str]]]:
        expected, expected_operation, response = next(controls)
        assert operation == expected_operation, expected
        token = "a" * 64 if expected == "ambiguous" else "b" * 64
        return token, expected, response

    direct = iter(
        (
            (200, {"operation": "pause", "status": "confirmed", "code": "confirmed"}),
            (409, {"operation": "cancel", "status": "denied", "code": "state_token_mismatch"}),
            (202, {"operation": "pause", "status": "outcome_unknown", "code": "outcome_unknown"}),
            (202, {"operation": "pause", "status": "outcome_unknown", "code": "outcome_unknown"}),
        )
    )

    monkeypatch.setattr(contract, "_request_json", request_json)
    monkeypatch.setattr(contract, "_start_test_print", start_test_print)
    monkeypatch.setattr(contract, "_wait_klove_phase", wait_phase)
    monkeypatch.setattr(contract, "_wait_moonraker_phase", lambda _phase: None)
    monkeypatch.setattr(contract, "_control_current", control_current)
    monkeypatch.setattr(contract, "_control", lambda *_args: next(direct))
    monkeypatch.setattr(contract, "_proxy_count", lambda operation: next(counts[operation]))
    monkeypatch.setattr(contract, "_save_state_token", saved.append)
    history = iter((("ABC123", 1.0), ("ABC123", 1.0), ("ABC123", 1.0)))
    monkeypatch.setattr(runner, "_history_identity", lambda _status: next(history))

    assert runner.main() == 0
    assert starts == 1
    assert saved == ["a" * 64]
