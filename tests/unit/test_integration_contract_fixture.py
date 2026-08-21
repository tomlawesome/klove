from __future__ import annotations

import importlib.util
import stat
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "integration" / "moonraker-sim" / "fixture"
RATOS_TOOL = ROOT / "tests" / "integration" / "ratos-emulation" / "tool.py"
ENVIRONMENT_NAMES = (
    "KLOVE_TEST_KLOVE_URL",
    "KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION",
    "KLOVE_TEST_MOONRAKER_HOST_HEADER",
    "KLOVE_TEST_MOONRAKER_PROXY_URL",
    "KLOVE_TEST_MOONRAKER_URL",
    "KLOVE_TEST_PROXY_CONTROL_LISTEN_HOST",
    "KLOVE_TEST_PROXY_CONTROL_LISTEN_PORT",
    "KLOVE_TEST_PROXY_CONTROL_URL",
    "KLOVE_TEST_PROXY_LISTEN_HOST",
    "KLOVE_TEST_PROXY_LISTEN_PORT",
    "KLOVE_TEST_PROXY_UPSTREAM_URL",
)


def _load_fixture(filename: str) -> ModuleType:
    module_name = f"_klove_fixture_{filename.removesuffix('.py')}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, FIXTURE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load integration fixture module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _load_ratos_tool() -> ModuleType:
    module_name = f"_klove_ratos_tool_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, RATOS_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load RatOS integration tool")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _load_dispatch_fixture() -> ModuleType:
    reset = _load_fixture("sdcard_reset.py")
    sys.modules["sdcard_reset"] = reset
    try:
        return _load_fixture("dispatch_contract.py")
    finally:
        sys.modules.pop("sdcard_reset", None)


def test_fixture_sdcard_reset_accepts_only_the_exact_ok_result() -> None:
    reset = _load_fixture("sdcard_reset.py")

    reset._require_exact_response(
        status=200,
        content_types=["application/json"],
        content_type="application/json",
        charset="UTF-8",
        body=b'{"result":"ok"}',
    )


@pytest.mark.parametrize(
    ("updates", "category"),
    (
        ({"status": 201}, "status"),
        ({"content_types": []}, "content_type"),
        ({"content_types": ["application/json", "application/json"]}, "content_type"),
        ({"content_type": "text/plain"}, "content_type"),
        ({"charset": "latin-1"}, "content_type"),
        ({"body": b"x" * 1025}, "size"),
        ({"body": b'{"result":{},"result":{}}'}, "duplicate"),
        ({"body": b'{"result":{}}'}, "shape"),
        ({"body": b'{"result":NaN}'}, "json"),
        ({"body": b"\xff"}, "json"),
    ),
)
def test_fixture_sdcard_reset_rejects_every_response_alias(
    updates: dict[str, object], category: str
) -> None:
    reset = _load_fixture("sdcard_reset.py")
    response: dict[str, object] = {
        "status": 200,
        "content_types": ["application/json"],
        "content_type": "application/json",
        "charset": None,
        "body": b'{"result":"ok"}',
    }
    response.update(updates)

    with pytest.raises(RuntimeError, match=f"response was invalid: {category}"):
        reset._require_exact_response(**response)


@pytest.mark.asyncio
async def test_dispatch_fixture_observes_the_durable_unknown_before_reconciliation() -> None:
    dispatch = _load_dispatch_fixture()
    request = SimpleNamespace(operation_id="operation", idempotency_key="idempotency")
    record = SimpleNamespace(state=dispatch.DispatchState.OUTCOME_UNKNOWN)
    journal = SimpleNamespace(lookup=lambda *_args: record)

    assert (
        await dispatch._wait_persisted_state(
            journal,
            request,
            dispatch.DispatchState.OUTCOME_UNKNOWN,
            timeout=0.1,
        )
        is record
    )


def test_ratos_upload_contract_accepts_exact_top_level_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_ratos_tool()
    content = b"controlled fixture\n"
    captured: dict[str, object] = {}

    def fake_http_json(path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"path": path, **kwargs})
        return {
            "item": {
                "root": "gcodes",
                "path": "contract.gcode",
                "modified": 1.0,
                "size": len(content),
                "permissions": "rw",
            },
            "print_started": False,
            "print_queued": False,
            "action": "create_file",
        }

    monkeypatch.setattr(tool, "_http_json", fake_http_json)
    tool._upload_contract_file(
        "fixture-key",
        root="gcodes",
        filename="contract.gcode",
        content=content,
        expect_print_status=True,
    )

    assert captured["path"] == "/server/files/upload"
    assert captured["expected_status"] == 201
    assert captured["method"] == "POST"


def test_ratos_http_uses_bounded_guest_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _load_ratos_tool()
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def read(self, limit: int) -> bytes:
            captured["limit"] = limit
            return b"{}"

    class Connection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            captured.update(host=host, port=port, timeout=timeout)

        def request(
            self,
            method: str,
            path: str,
            *,
            body: bytes | None,
            headers: dict[str, str],
        ) -> None:
            captured.update(method=method, path=path, body=body, headers=headers)

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(tool.http.client, "HTTPConnection", Connection)

    assert tool._http_request(18080, "/server/info") == (200, b"{}")
    assert captured["timeout"] == 60
    assert captured["closed"] is True


def test_ratos_contract_prepare_uses_a_dedicated_restart_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = _load_ratos_tool()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_http_json(path: str, **kwargs: object) -> dict[str, object]:
        calls.append((path, kwargs))
        if path == "/printer/restart":
            return {"result": "ok"}
        return {"result": {"status": {"print_stats": {"state": "standby"}}}}

    monkeypatch.setattr(tool, "SECRETS", secrets)
    monkeypatch.setattr(tool, "_require_secret_directory", lambda: None)
    monkeypatch.setattr(tool, "_contract_source", lambda _path: b"fixture")
    monkeypatch.setattr(tool, "_moonraker_api_key", lambda: "a" * 32)
    monkeypatch.setattr(tool, "_upload_contract_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, "_verify_remote_contract_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, "_http_json", fake_http_json)
    monkeypatch.setattr(tool, "_http_request", lambda *args, **kwargs: (401, b"{}"))
    monkeypatch.setattr(tool, "_wait_printer_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, "_replace_moonraker_configuration", lambda *_args, **_kwargs: b"cfg")
    monkeypatch.setattr(tool, "_contract_status", lambda *_args, **_kwargs: {"phase": "standby"})
    monkeypatch.setattr(tool, "_contract_identity", lambda path: {"name": path.name})
    monkeypatch.setattr(tool, "_write_private", lambda *args, **kwargs: None)

    tool.contract_prepare()

    restart_timeouts = [kwargs["timeout"] for path, kwargs in calls if path == "/printer/restart"]
    assert restart_timeouts == [tool.PRINTER_RESTART_TIMEOUT_SECONDS] * 2
    assert tool.PRINTER_RESTART_TIMEOUT_SECONDS > tool.HTTP_TIMEOUT_SECONDS


def test_ratos_contract_prepare_does_not_retry_a_timed_out_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = _load_ratos_tool()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_http_json(path: str, **kwargs: object) -> dict[str, object]:
        calls.append((path, kwargs))
        if path == "/printer/restart":
            raise RuntimeError("/printer/restart timed out")
        return {"result": {"status": {"print_stats": {"state": "standby"}}}}

    monkeypatch.setattr(tool, "SECRETS", secrets)
    monkeypatch.setattr(tool, "_require_secret_directory", lambda: None)
    monkeypatch.setattr(tool, "_contract_source", lambda _path: b"fixture")
    monkeypatch.setattr(tool, "_moonraker_api_key", lambda: "a" * 32)
    monkeypatch.setattr(tool, "_upload_contract_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, "_verify_remote_contract_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, "_http_json", fake_http_json)
    monkeypatch.setattr(tool, "_http_request", lambda *args, **kwargs: (401, b"{}"))
    monkeypatch.setattr(tool, "_wait_printer_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, "_replace_moonraker_configuration", lambda *_args, **_kwargs: b"cfg")
    monkeypatch.setattr(tool, "_contract_status", lambda *_args, **_kwargs: {"phase": "standby"})
    monkeypatch.setattr(tool, "_contract_identity", lambda path: {"name": path.name})
    monkeypatch.setattr(tool, "_write_private", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="timed out"):
        tool.contract_prepare()

    restart_calls = [path for path, _kwargs in calls if path == "/printer/restart"]
    assert restart_calls == ["/printer/restart"]


def test_ratos_contract_prepare_rejects_nonempty_secret_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = _load_ratos_tool()
    (tmp_path / "unexpected").write_text("material", encoding="utf-8")
    monkeypatch.setattr(tool, "SECRETS", tmp_path)
    monkeypatch.setattr(tool, "_require_secret_directory", lambda: None)

    with pytest.raises(RuntimeError, match="empty before preparation"):
        tool.contract_prepare()


def test_ratos_secret_read_requires_exact_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = _load_ratos_tool()
    secret = tmp_path / "moonraker-api-key"
    secret.write_text("fixture-value\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setattr(tool, "SECRETS", tmp_path)
    exact = SimpleNamespace(st_uid=10001, st_gid=10001, st_mode=stat.S_IFREG | 0o600)
    monkeypatch.setattr(tool, "_regular_file", lambda _path: exact)

    assert tool._read_secret("moonraker-api-key") == "fixture-value"
    exact.st_uid = 10000
    with pytest.raises(RuntimeError, match="identity or mode"):
        tool._read_secret("moonraker-api-key")


@pytest.mark.parametrize(
    "mutation",
    (
        {"result": {}},
        {"print_started": True},
        {"print_queued": True},
        {"action": "modify_file"},
        {"item": {"size": 1}},
        {"item": {"permissions": "r"}},
    ),
)
def test_ratos_upload_contract_rejects_ambiguous_responses(
    monkeypatch: pytest.MonkeyPatch, mutation: dict[str, object]
) -> None:
    tool = _load_ratos_tool()
    content = b"controlled fixture\n"
    response: dict[str, object] = {
        "item": {
            "root": "gcodes",
            "path": "contract.gcode",
            "modified": 1.0,
            "size": len(content),
            "permissions": "rw",
        },
        "print_started": False,
        "print_queued": False,
        "action": "create_file",
    }
    nested_item = mutation.get("item")
    if isinstance(nested_item, dict):
        response["item"] = {**response["item"], **nested_item}  # type: ignore[dict-item]
    else:
        response.update(mutation)
    monkeypatch.setattr(tool, "_http_json", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError):
        tool._upload_contract_file(
            "fixture-key",
            root="gcodes",
            filename="contract.gcode",
            content=content,
            expect_print_status=True,
        )


def test_ratos_upload_contract_accepts_exact_config_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_ratos_tool()
    content = b"[printer]\nkinematics: none\n"
    response = {
        "item": {
            "root": "config",
            "path": "printer.cfg",
            "modified": 1.0,
            "size": len(content),
            "permissions": "rw",
        },
        "action": "create_file",
    }
    monkeypatch.setattr(tool, "_http_json", lambda *_args, **_kwargs: response)

    tool._upload_contract_file(
        "fixture-key",
        root="config",
        filename="printer.cfg",
        content=content,
        expect_print_status=False,
    )


def _clear_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_contract_runner_defaults_and_exact_host_header_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    runner = _load_fixture("exercise_contract.py")

    assert runner.KLOVE == "http://klove:8080"
    assert runner.MOONRAKER == "http://printer-host:7125"
    assert runner.MOONRAKER_PROXY == "http://moonraker-proxy:7125"
    assert runner.PROXY_CONTROL == "http://moonraker-proxy:9126"
    assert runner.MOONRAKER_AUTH_EXPECTATION == "rejected"
    assert "Host" not in runner._moonraker_headers(api_key="test", direct=True)

    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_HOST_HEADER", "ratos.local")
    runner = _load_fixture("exercise_contract.py")
    assert runner._moonraker_headers(api_key="test", direct=True)["Host"] == "ratos.local"
    assert "Host" not in runner._moonraker_headers(api_key="test", direct=False)
    assert "Host" not in runner._klove_headers(token=uuid.uuid4().hex)


def test_contract_runner_accepts_exact_origin_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv("KLOVE_TEST_KLOVE_URL", "https://127.0.0.1:8443/")
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_PROXY_URL", "http://127.0.0.1:27125/")
    monkeypatch.setenv("KLOVE_TEST_PROXY_CONTROL_URL", "http://127.0.0.1:9126")

    runner = _load_fixture("exercise_contract.py")

    assert runner.KLOVE == "https://127.0.0.1:8443"
    assert runner.MOONRAKER == "http://127.0.0.1:18080"
    assert runner.MOONRAKER_PROXY == "http://127.0.0.1:27125"
    assert runner.PROXY_CONTROL == "http://127.0.0.1:9126"


def test_contract_runner_accepts_explicit_trusted_moonraker_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION", "trusted")

    runner = _load_fixture("exercise_contract.py")

    assert runner.MOONRAKER_AUTH_EXPECTATION == "trusted"


def test_contract_runner_rejects_unknown_moonraker_auth_expectation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION", "maybe")

    with pytest.raises(ValueError, match="KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION"):
        _load_fixture("exercise_contract.py")


def test_contract_runner_requires_exact_trusted_transport_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION", "trusted")
    runner = _load_fixture("exercise_contract.py")
    responses = iter(
        (
            (401, {}),
            (
                200,
                {"result": {"status": {"print_stats": {"state": "standby"}}}},
            ),
        )
    )

    class ContractReached(RuntimeError):
        pass

    monkeypatch.setattr(runner, "_request_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        runner,
        "_start_test_print",
        lambda: (_ for _ in ()).throw(ContractReached),
    )

    with pytest.raises(ContractReached):
        runner.main()


def test_contract_runner_retries_only_explicit_predispatch_denials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    runner = _load_fixture("exercise_contract.py")
    snapshot = {"phase": "printing", "state_token": "a" * 64}
    responses = iter(
        (
            (
                409,
                {
                    "operation": "pause",
                    "status": "denied",
                    "code": "job_identity_unavailable",
                },
            ),
            (
                409,
                {
                    "operation": "pause",
                    "status": "denied",
                    "code": "state_token_mismatch",
                },
            ),
            (
                202,
                {
                    "operation": "pause",
                    "status": "outcome_unknown",
                    "code": "outcome_unknown",
                },
            ),
        )
    )
    calls: list[tuple[str, str, str]] = []

    def control(operation: str, token: str, key: str) -> tuple[int, dict[str, str]]:
        calls.append((operation, token, key))
        return next(responses)

    monkeypatch.setattr(runner, "_snapshot", lambda: snapshot)
    monkeypatch.setattr(runner, "_control", control)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    token, _key, response = runner._control_current("pause", "printing")

    assert token == "a" * 64
    assert response[0] == 202
    assert len(calls) == 3
    assert len({key for _operation, _token, key in calls}) == 3


def test_contract_runner_never_retries_postdispatch_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    runner = _load_fixture("exercise_contract.py")
    monkeypatch.setattr(
        runner,
        "_snapshot",
        lambda: {"phase": "printing", "state_token": "a" * 64},
    )
    calls = 0

    def uncertain(_operation: str, _token: str, _key: str) -> tuple[int, dict[str, str]]:
        nonlocal calls
        calls += 1
        return 202, {
            "operation": "pause",
            "status": "outcome_unknown",
            "code": "outcome_unknown",
        }

    monkeypatch.setattr(runner, "_control", uncertain)

    _token, _key, response = runner._control_current("pause", "printing")

    assert response[0] == 202
    assert calls == 1


def test_contract_runner_does_not_retry_other_denials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    runner = _load_fixture("exercise_contract.py")
    monkeypatch.setattr(
        runner,
        "_snapshot",
        lambda: {"phase": "printing", "state_token": "a" * 64},
    )
    calls = 0

    def denied(_operation: str, _token: str, _key: str) -> tuple[int, dict[str, str]]:
        nonlocal calls
        calls += 1
        return 409, {
            "operation": "pause",
            "status": "denied",
            "code": "preflight_unavailable",
        }

    monkeypatch.setattr(runner, "_control", denied)

    _token, _key, response = runner._control_current("pause", "printing")

    assert response[1]["code"] == "preflight_unavailable"
    assert calls == 1


@pytest.mark.parametrize(
    "value",
    (
        "ftp://printer-host:7125",
        "http://user:password@printer-host:7125",
        "http://printer-host:0",
        "http://printer-host:7125/path",
        "http://printer-host:7125?query=yes",
        "http://printer-host:7125#fragment",
    ),
)
def test_contract_runner_rejects_ambiguous_origins(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_URL", value)
    with pytest.raises(ValueError, match="KLOVE_TEST_MOONRAKER_URL"):
        _load_fixture("exercise_contract.py")


@pytest.mark.parametrize(
    "value",
    ("bad host", "ratos.local\r\nInjected: yes", "éxample", "bad/path", "user@host"),
)
def test_contract_runner_rejects_invalid_host_headers(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_HOST_HEADER", value)
    with pytest.raises(ValueError, match="KLOVE_TEST_MOONRAKER_HOST_HEADER"):
        _load_fixture("exercise_contract.py")


def test_proxy_defaults_and_valid_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_environment(monkeypatch)
    proxy = _load_fixture("proxy.py")
    assert (proxy.PROXY_LISTEN_HOST, proxy.PROXY_LISTEN_PORT) == (
        "0.0.0.0",  # noqa: S104 -- asserted native fixture default.
        7125,
    )
    assert (proxy.CONTROL_LISTEN_HOST, proxy.CONTROL_LISTEN_PORT) == (
        "0.0.0.0",  # noqa: S104 -- asserted native fixture default.
        9126,
    )
    assert (proxy.TARGET_HOST, proxy.TARGET_PORT, proxy.TARGET_TLS) == (
        "printer-host",
        7125,
        False,
    )

    monkeypatch.setenv("KLOVE_TEST_PROXY_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("KLOVE_TEST_PROXY_LISTEN_PORT", "27125")
    monkeypatch.setenv("KLOVE_TEST_PROXY_CONTROL_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("KLOVE_TEST_PROXY_CONTROL_LISTEN_PORT", "9127")
    monkeypatch.setenv("KLOVE_TEST_PROXY_UPSTREAM_URL", "https://moonraker.local:8443/")
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_HOST_HEADER", "ratos.local")
    proxy = _load_fixture("proxy.py")
    assert (proxy.PROXY_LISTEN_HOST, proxy.PROXY_LISTEN_PORT) == ("127.0.0.1", 27125)
    assert (proxy.CONTROL_LISTEN_HOST, proxy.CONTROL_LISTEN_PORT) == ("127.0.0.1", 9127)
    assert (proxy.TARGET_HOST, proxy.TARGET_PORT, proxy.TARGET_TLS) == (
        "moonraker.local",
        8443,
        True,
    )
    assert proxy.TARGET_HOST_HEADER == "ratos.local"


def test_proxy_rewrites_only_the_upstream_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv("KLOVE_TEST_MOONRAKER_HOST_HEADER", "ratos.local")
    proxy = _load_fixture("proxy.py")
    head = b"GET /server/info HTTP/1.1\r\nHost: old.local\r\nConnection: keep-alive\r\n\r\n"

    assert proxy._rewrite_head(head, close=True) == (
        b"GET /server/info HTTP/1.1\r\nHost: ratos.local\r\nConnection: close\r\n\r\n"
    )
    assert proxy._rewrite_head(head, close=False) == (
        b"GET /server/info HTTP/1.1\r\nHost: ratos.local\r\nConnection: keep-alive\r\n\r\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{}",
    ),
)
async def test_proxy_relays_bounded_http_response_framing(
    monkeypatch: pytest.MonkeyPatch, response: bytes
) -> None:
    _clear_environment(monkeypatch)
    proxy = _load_fixture("proxy.py")
    reader = proxy.asyncio.StreamReader()
    reader.feed_data(response)
    reader.feed_eof()

    assert await proxy._read_response(reader) == response


@pytest.mark.asyncio
async def test_proxy_relays_bounded_chunked_upload_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    proxy = _load_fixture("proxy.py")
    body = b"4\r\ntest\r\n0\r\n\r\n"
    reader = proxy.asyncio.StreamReader()
    reader.feed_data(body)
    reader.feed_eof()

    assert await proxy._read_request_body(reader, {"transfer-encoding": "chunked"}) == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    (
        {"content-length": "1", "transfer-encoding": "chunked"},
        {"transfer-encoding": "gzip"},
        {"content-length": "-1"},
    ),
)
async def test_proxy_rejects_ambiguous_or_invalid_request_framing(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    _clear_environment(monkeypatch)
    proxy = _load_fixture("proxy.py")
    reader = proxy.asyncio.StreamReader()
    reader.feed_eof()

    with pytest.raises(ValueError):
        await proxy._read_request_body(reader, headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n+2\r\n{}\r\n0\r\n\r\n",
    ),
)
async def test_proxy_rejects_ambiguous_http_response_framing(
    monkeypatch: pytest.MonkeyPatch, response: bytes
) -> None:
    _clear_environment(monkeypatch)
    proxy = _load_fixture("proxy.py")
    reader = proxy.asyncio.StreamReader()
    reader.feed_data(response)
    reader.feed_eof()

    with pytest.raises(ValueError):
        await proxy._read_response(reader)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("KLOVE_TEST_PROXY_LISTEN_HOST", "printer-host"),
        ("KLOVE_TEST_PROXY_LISTEN_PORT", "0"),
        ("KLOVE_TEST_PROXY_LISTEN_PORT", "65536"),
        ("KLOVE_TEST_PROXY_LISTEN_PORT", "12x"),
        ("KLOVE_TEST_PROXY_UPSTREAM_URL", "http://user:pass@printer-host:7125"),
        ("KLOVE_TEST_PROXY_UPSTREAM_URL", "http://printer-host:7125/path"),
        ("KLOVE_TEST_MOONRAKER_HOST_HEADER", "bad host"),
        ("KLOVE_TEST_MOONRAKER_HOST_HEADER", "bad/path"),
    ),
)
def test_proxy_rejects_invalid_overrides(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        _load_fixture("proxy.py")
