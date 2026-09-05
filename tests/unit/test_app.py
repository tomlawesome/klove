from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

import klove.app as app_module
from klove.app import serve
from klove.domain.onboarding import MoonrakerEndpoint, PrinterIdentityEvidence
from klove.northbound.api import (
    create_api,
    owner_authenticator_key,
    owner_sessions_key,
    ready_key,
)
from klove.orchestration.bootstrap import FileBootstrapError

from ..onboarding_helpers import PRINTER_UUID, identity


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def free_ports(count: int) -> tuple[int, ...]:
    listeners: list[socket.socket] = []
    try:
        for _index in range(count):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return tuple(int(listener.getsockname()[1]) for listener in listeners)
    finally:
        for listener in listeners:
            listener.close()


def write_config(  # noqa: PLR0913 -- test fixture accepts explicit deployment toggles.
    tmp_path: Path,
    port: int,
    *,
    printer: bool,
    control: bool = False,
    onboarding: bool = False,
    frame: bool = False,
    bridge: bool = False,
) -> Path:
    api_token = tmp_path / "api.token"
    api_token.write_text("a" * 32, encoding="utf-8")
    state_directory = tmp_path / f"state-{port}"
    state_directory.mkdir(mode=0o700)
    secret_directory = state_directory / "registry-secrets"
    secret_directory.mkdir(mode=0o700)
    printer_block = ""
    onboarding_block = ""
    bridge_block = ""
    if bridge:
        staging_directory = state_directory / "ftps-staging"
        staging_directory.mkdir(mode=0o700)
        mqtt_port, control_port, passive_port = free_ports(3)
        bridge_block = f"""

[grove_bridge]
enabled = true
listen_host = "127.0.0.1"
mqtt_port = {mqtt_port}
ftps_control_port = {control_port}
ftps_passive_port_min = {passive_port}
ftps_passive_port_max = {passive_port}
ftps_advertised_ipv4 = "127.0.0.1"
tls_certificate_file = "{(tmp_path / "bridge.crt").as_posix()}"
tls_private_key_file = "{(tmp_path / "bridge.key").as_posix()}"
mqtt_journal_file = "{(state_directory / "mqtt.sqlite3").as_posix()}"
staging_directory = "{staging_directory.as_posix()}"
"""
    if onboarding:
        owner_token = tmp_path / "owner.token"
        owner_token.write_text("o" * 32, encoding="utf-8")
        onboarding_block = f"""

[onboarding]
enabled = true
owner_credential_file = "{owner_token.as_posix()}"
allowed_grove_origins = ["https://grove.example.invalid"]
{
            '''frame_origin = "https://grove.example.invalid:8443"
compatibility_host = "klove.example.invalid"'''
            if frame
            else ""
        }
"""
    if printer:
        moonraker_token = tmp_path / "moonraker.token"
        moonraker_token.write_text("m" * 32, encoding="utf-8")
        printer_block = f"""

[[printers]]
id = "voron"
uuid = "11111111-1111-4111-8111-111111111111"
endpoint = "http://127.0.0.1:7125"
api_key_file = "{moonraker_token.as_posix()}"
verify_tls = false
control_enabled = {str(control).lower()}
"""
    config = tmp_path / f"config-{port}.toml"
    config.write_text(
        f"""
[api]
listen_host = "127.0.0.1"
listen_port = {port}
token_file = "{api_token.as_posix()}"

[registry]
database_file = "{(state_directory / "registry.sqlite3").as_posix()}"
secret_directory = "{secret_directory.as_posix()}"
allowed_probe_cidrs = ["127.0.0.0/8"]

[dispatch]
journal_file = "{(state_directory / "start.sqlite3").as_posix()}"

[control]
enabled = {str(control).lower()}
{onboarding_block}
{bridge_block}
{printer_block}
""".strip(),
        encoding="utf-8",
    )
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "expected_control_status", "frame"),
    [(True, 400, False), (False, 401, False), (False, 401, True)],
)
async def test_serve_starts_api_before_monitors_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: bool,
    expected_control_status: int,
    frame: bool,
) -> None:
    started = asyncio.Event()
    site_started = asyncio.Event()
    application_ready = asyncio.Event()
    applications: list[Any] = []
    observed_startup_order: list[tuple[bool, bool]] = []

    real_site = web.TCPSite
    real_create_api = create_api

    class InstrumentedSite:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._site = real_site(*args, **kwargs)

        async def start(self) -> None:
            # Force a scheduling point that made the former monitor-first order observable.
            await asyncio.sleep(0)
            await self._site.start()
            site_started.set()

    def capture_application(*args: Any, **kwargs: Any) -> Any:
        application = real_create_api(*args, **kwargs)
        applications.append(application)
        return application

    class FakeMonitor:
        def __init__(self, *_args: Any) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            observed_startup_order.append((site_started.is_set(), applications[0][ready_key].ready))
            started.set()
            await stop.wait()

    class FakeProbe:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def probe(
            self,
            _endpoint: MoonrakerEndpoint,
            _api_key: str,
        ) -> PrinterIdentityEvidence:
            return identity()

    class Logger:
        def info(self, *_args: object) -> None:
            application_ready.set()

    monkeypatch.setattr(web, "TCPSite", InstrumentedSite)
    monkeypatch.setattr("klove.app.create_api", capture_application)
    monkeypatch.setattr(app_module, "MoonrakerMonitor", FakeMonitor)
    monkeypatch.setattr(app_module, "MoonrakerOnboardingProbe", FakeProbe)
    monkeypatch.setattr(app_module, "LOGGER", Logger())
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(
            write_config(
                tmp_path,
                port,
                printer=True,
                control=control,
                onboarding=True,
                frame=frame,
            ),
            stop,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    assert observed_startup_order == [(True, False)]

    await asyncio.wait_for(application_ready.wait(), timeout=2)
    assert applications[0][ready_key].ready is True
    assert applications[0][owner_authenticator_key].authenticate("o" * 32)
    assert applications[0][owner_sessions_key].active_count == 0
    if frame:
        assert any(
            resource.canonical == "/v1/onboarding/frame/completion"
            for resource in applications[0].router.resources()
        )

    async with aiohttp.ClientSession() as session:
        response = await session.get(f"http://127.0.0.1:{port}/health/ready")
        assert response.status == 200
        assert await response.json() == {"status": "ready"}
        control_response = await session.post(
            f"http://127.0.0.1:{port}/v1/printers/{PRINTER_UUID}/commands/pause",
            headers={"Authorization": f"Bearer {'a' * 32}"},
        )
        assert control_response.status == expected_control_status

    stop.set()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_serve_creates_its_own_stop_event(tmp_path: Path) -> None:
    port = free_port()
    task = asyncio.create_task(serve(write_config(tmp_path, port, printer=False)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_restart_reuses_one_exact_bootstrap_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor_starts = 0
    probe_calls = 0
    started = asyncio.Event()

    class FakeMonitor:
        def __init__(self, *_args: Any) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            nonlocal monitor_starts
            monitor_starts += 1
            started.set()
            await stop.wait()

    class FakeProbe:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def probe(
            self,
            _endpoint: MoonrakerEndpoint,
            _api_key: str,
        ) -> PrinterIdentityEvidence:
            nonlocal probe_calls
            probe_calls += 1
            return identity()

    monkeypatch.setattr(app_module, "MoonrakerMonitor", FakeMonitor)
    monkeypatch.setattr(app_module, "MoonrakerOnboardingProbe", FakeProbe)
    port = free_port()
    config = write_config(tmp_path, port, printer=True)

    for _attempt in range(2):
        started.clear()
        stop = asyncio.Event()
        task = asyncio.create_task(serve(config, stop))
        await asyncio.wait_for(started.wait(), timeout=2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    assert probe_calls == 1
    assert monitor_starts == 2


@pytest.mark.asyncio
async def test_enabled_ftps_bridge_recovers_seeds_starts_then_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    applications: list[Any] = []
    captured: dict[str, Any] = {}
    started = asyncio.Event()

    class Sessions:
        def __init__(self) -> None:
            events.append("sessions")

        def fence_all(self) -> None:
            events.append("fence")

    class Authenticator:
        def __init__(self, store: object, secrets: object, admissions: object) -> None:
            events.append("authenticator")
            captured.update(store=store, secrets=secrets, admissions=admissions)

    class Staging:
        def __init__(self, directory: Path, *, limits: object, capacity: int) -> None:
            events.append("staging")
            captured.update(directory=directory, limits=limits, capacity=capacity)

        def initialize(self) -> None:
            events.append("initialize")

        def reconcile(self) -> None:
            events.append("reconcile")

    class Runtime:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            events.append("runtime")
            captured["observer"] = kwargs["committed_record_observer"]
            captured["runtime_admissions"] = kwargs["admissions"]

        async def reconcile_committed(self, _record: object) -> None:
            raise AssertionError("no bootstrap record expected")

        async def refresh(self) -> tuple[str, ...]:
            events.append("refresh")
            return ()

        async def shutdown(self) -> None:
            events.append("runtime_shutdown")

    class Ftps:
        def __init__(
            self,
            _config: object,
            authenticator: object,
            staging: object,
            admissions: object,
            *,
            session_registry: object,
        ) -> None:
            events.append("ftps")
            captured.update(
                ftps_authenticator=authenticator,
                ftps_staging=staging,
                ftps_admissions=admissions,
                ftps_sessions=session_registry,
            )

        async def start(self) -> None:
            assert applications[0][ready_key].ready is False
            events.append("ftps_start")
            started.set()

        async def close(self) -> None:
            events.append("ftps_close")

    class Site:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def start(self) -> None:
            assert applications[0][ready_key].ready is False
            events.append("site_start")

    real_create_api = create_api

    def capture_application(*args: Any, **kwargs: Any) -> Any:
        application = real_create_api(*args, **kwargs)
        applications.append(application)
        return application

    monkeypatch.setattr(app_module, "CompatibilitySessionRegistry", Sessions)
    monkeypatch.setattr(app_module, "CompatibilityAuthenticator", Authenticator)
    monkeypatch.setattr(app_module, "FtpsStagingStore", Staging)
    monkeypatch.setattr(app_module, "RegistryRuntimeSupervisor", Runtime)
    monkeypatch.setattr(app_module, "FtpsTlsServer", Ftps)
    monkeypatch.setattr(web, "TCPSite", Site)
    monkeypatch.setattr(app_module, "create_api", capture_application)

    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(write_config(tmp_path, free_port(), printer=False, bridge=True), stop)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0)

    assert applications[0][ready_key].ready is True
    assert events[:8] == [
        "sessions",
        "authenticator",
        "staging",
        "initialize",
        "reconcile",
        "runtime",
        "site_start",
        "ftps",
    ]
    assert events[8:10] == ["refresh", "ftps_start"]
    assert captured["observer"] is captured["ftps_sessions"]
    assert captured["admissions"] is captured["runtime_admissions"]
    assert captured["admissions"] is captured["ftps_admissions"]
    assert captured["ftps_authenticator"].__class__ is Authenticator
    assert captured["ftps_staging"].__class__ is Staging
    assert captured["capacity"] == 4096

    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert events[-4:] == ["ftps_close", "ftps_close", "fence", "runtime_shutdown"]


@pytest.mark.asyncio
async def test_disabled_bridge_never_constructs_or_touches_bridge_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled bridge was touched")

    monkeypatch.setattr(app_module, "CompatibilitySessionRegistry", forbidden)
    monkeypatch.setattr(app_module, "CompatibilityAuthenticator", forbidden)
    monkeypatch.setattr(app_module, "FtpsStagingStore", forbidden)
    monkeypatch.setattr(app_module, "FtpsTlsServer", forbidden)

    stop = asyncio.Event()
    task = asyncio.create_task(serve(write_config(tmp_path, free_port(), printer=False), stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_ftps_start_failure_stays_original_and_cleanup_attempts_every_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class StartFailure(RuntimeError):
        pass

    class CloseFailure(RuntimeError):
        pass

    class Staging:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def initialize(self) -> None:
            pass

        def reconcile(self) -> None:
            pass

    class Sessions:
        def fence_all(self) -> None:
            events.append("fence")

    class Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def reconcile_committed(self, _record: object) -> None:
            raise AssertionError

        async def refresh(self) -> tuple[str, ...]:
            events.append("refresh")
            return ()

        async def shutdown(self) -> None:
            events.append("runtime_shutdown")

    class Ftps:
        close_calls = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def start(self) -> None:
            raise StartFailure("fixed")

        async def close(self) -> None:
            type(self).close_calls += 1
            events.append("ftps_close")
            if type(self).close_calls == 1:
                raise CloseFailure("cleanup")

    class Runner(web.AppRunner):
        async def cleanup(self) -> None:
            events.append("runner_cleanup")
            await super().cleanup()

    monkeypatch.setattr(app_module, "CompatibilitySessionRegistry", Sessions)
    monkeypatch.setattr(app_module, "CompatibilityAuthenticator", lambda *_args: object())
    monkeypatch.setattr(app_module, "FtpsStagingStore", Staging)
    monkeypatch.setattr(app_module, "RegistryRuntimeSupervisor", Runtime)
    monkeypatch.setattr(app_module, "FtpsTlsServer", Ftps)
    monkeypatch.setattr(web, "AppRunner", Runner)

    with pytest.raises(StartFailure, match="fixed") as caught:
        await serve(write_config(tmp_path, free_port(), printer=False, bridge=True))

    assert caught.value.__notes__ == ["runtime cleanup also failed"]
    assert events[-5:] == [
        "refresh",
        "ftps_close",
        "ftps_close",
        "fence",
        "runtime_shutdown",
    ] or events[-6:] == [
        "refresh",
        "ftps_close",
        "ftps_close",
        "fence",
        "runtime_shutdown",
        "runner_cleanup",
    ]
    assert "runner_cleanup" in events


@pytest.mark.asyncio
async def test_cancellation_waits_for_complete_independent_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    ready = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class Staging:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def initialize(self) -> None:
            pass

        def reconcile(self) -> None:
            pass

    class Sessions:
        def fence_all(self) -> None:
            events.append("fence")

    class Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def reconcile_committed(self, _record: object) -> None:
            raise AssertionError

        async def refresh(self) -> tuple[str, ...]:
            return ()

        async def shutdown(self) -> None:
            events.append("runtime_shutdown")

    class Ftps:
        close_calls = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def start(self) -> None:
            ready.set()

        async def close(self) -> None:
            type(self).close_calls += 1
            events.append("ftps_close")
            if type(self).close_calls == 1:
                cleanup_started.set()
                await release_cleanup.wait()

    monkeypatch.setattr(app_module, "CompatibilitySessionRegistry", Sessions)
    monkeypatch.setattr(app_module, "CompatibilityAuthenticator", lambda *_args: object())
    monkeypatch.setattr(app_module, "FtpsStagingStore", Staging)
    monkeypatch.setattr(app_module, "RegistryRuntimeSupervisor", Runtime)
    monkeypatch.setattr(app_module, "FtpsTlsServer", Ftps)

    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(write_config(tmp_path, free_port(), printer=False, bridge=True), stop)
    )
    await asyncio.wait_for(ready.wait(), timeout=2)
    stop.set()
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert events[-4:] == ["ftps_close", "ftps_close", "fence", "runtime_shutdown"]


@pytest.mark.asyncio
async def test_cleanup_failure_attempts_all_later_phases_and_preserves_first() -> None:
    events: list[str] = []

    class Failure(RuntimeError):
        pass

    first = Failure("ftps")

    class Ftps:
        async def close(self) -> None:
            events.append("ftps")
            raise first

    class Sessions:
        def fence_all(self) -> None:
            events.append("fence")
            raise Failure("fence")

    class Runtime:
        async def shutdown(self) -> None:
            events.append("runtime")
            raise Failure("runtime")

    class Runner:
        async def cleanup(self) -> None:
            events.append("runner")
            raise Failure("runner")

    class ReadyState:
        ready = True

    application = {ready_key: ReadyState()}
    stop = asyncio.Event()
    result = await app_module._cleanup_runtime(
        app_module._CleanupResources(
            application,  # type: ignore[arg-type]
            stop,
            Ftps(),  # type: ignore[arg-type]
            Sessions(),  # type: ignore[arg-type]
            Runtime(),  # type: ignore[arg-type]
            Runner(),  # type: ignore[arg-type]
        )
    )

    assert result is first
    assert application[ready_key].ready is False
    assert stop.is_set()
    assert events == ["ftps", "ftps", "fence", "runtime", "runner"]


@pytest.mark.asyncio
async def test_cleanup_failure_without_active_request_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ShutdownFailure(RuntimeError):
        pass

    class Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def reconcile_committed(self, _record: object) -> None:
            raise AssertionError

        async def refresh(self) -> tuple[str, ...]:
            return ()

        async def shutdown(self) -> None:
            raise ShutdownFailure("shutdown")

    monkeypatch.setattr(app_module, "RegistryRuntimeSupervisor", Runtime)
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(ShutdownFailure, match="shutdown"):
        await serve(write_config(tmp_path, free_port(), printer=False), stop)


@pytest.mark.asyncio
async def test_session_fence_failure_is_retained_without_skipping_shutdown() -> None:
    events: list[str] = []

    class FenceFailure(RuntimeError):
        pass

    failure = FenceFailure("fence")

    class Sessions:
        def fence_all(self) -> None:
            raise failure

    class Runtime:
        async def shutdown(self) -> None:
            events.append("runtime")

    class Runner:
        async def cleanup(self) -> None:
            events.append("runner")

    class ReadyState:
        ready = True

    result = await app_module._cleanup_runtime(
        app_module._CleanupResources(
            {ready_key: ReadyState()},  # type: ignore[arg-type]
            asyncio.Event(),
            None,
            Sessions(),  # type: ignore[arg-type]
            Runtime(),  # type: ignore[arg-type]
            Runner(),  # type: ignore[arg-type]
        )
    )

    assert result is failure
    assert events == ["runtime", "runner"]


@pytest.mark.asyncio
async def test_partial_bootstrap_failure_fences_runtime_before_any_ftps_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    applications: list[Any] = []
    second_probe_started = asyncio.Event()
    release_second_probe = asyncio.Event()

    class Sessions:
        def fence_all(self) -> None:
            events.append("fence")

    class Staging:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def initialize(self) -> None:
            pass

        def reconcile(self) -> None:
            pass

    class Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def reconcile_committed(self, record: Any) -> None:
            events.append(f"committed:{record.printer_uuid}")

        async def refresh(self) -> tuple[str, ...]:
            raise AssertionError("refresh must not follow failed bootstrap")

        async def shutdown(self) -> None:
            events.append("runtime_shutdown")

    class Probe:
        calls = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def probe(
            self,
            _endpoint: MoonrakerEndpoint,
            _api_key: str,
        ) -> PrinterIdentityEvidence:
            type(self).calls += 1
            if type(self).calls == 2:
                second_probe_started.set()
                await release_second_probe.wait()
                raise RuntimeError("second probe failed")
            return identity()

    def forbidden_ftps(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("FTPS constructed after failed bootstrap")

    real_create_api = create_api

    def capture_application(*args: Any, **kwargs: Any) -> Any:
        application = real_create_api(*args, **kwargs)
        applications.append(application)
        return application

    monkeypatch.setattr(app_module, "CompatibilitySessionRegistry", Sessions)
    monkeypatch.setattr(app_module, "CompatibilityAuthenticator", lambda *_args: object())
    monkeypatch.setattr(app_module, "FtpsStagingStore", Staging)
    monkeypatch.setattr(app_module, "RegistryRuntimeSupervisor", Runtime)
    monkeypatch.setattr(app_module, "MoonrakerOnboardingProbe", Probe)
    monkeypatch.setattr(app_module, "FtpsTlsServer", forbidden_ftps)
    monkeypatch.setattr(app_module, "create_api", capture_application)

    port = free_port()
    config = write_config(tmp_path, port, printer=True, bridge=True)
    second_token = tmp_path / "moonraker-second.token"
    second_token.write_text("n" * 32, encoding="utf-8")
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"""

[[printers]]
id = "second"
uuid = "22222222-2222-4222-8222-222222222222"
endpoint = "http://127.0.0.1:7126"
api_key_file = "{second_token.as_posix()}"
verify_tls = false
""",
        encoding="utf-8",
    )

    task = asyncio.create_task(serve(config))
    await asyncio.wait_for(second_probe_started.wait(), timeout=2)
    async with aiohttp.ClientSession() as session:
        response = await session.get(f"http://127.0.0.1:{port}/health/ready")
        assert response.status == 503
        assert await response.json() == {"status": "starting"}
    release_second_probe.set()
    with pytest.raises(FileBootstrapError):
        await task

    assert events == [f"committed:{PRINTER_UUID}", "fence", "runtime_shutdown"]
    assert applications[0][ready_key].ready is False
