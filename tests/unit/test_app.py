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

from ..onboarding_helpers import PRINTER_UUID, identity


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def write_config(
    tmp_path: Path,
    port: int,
    *,
    printer: bool,
    control: bool = False,
    onboarding: bool = False,
) -> Path:
    api_token = tmp_path / "api.token"
    api_token.write_text("a" * 32, encoding="utf-8")
    state_directory = tmp_path / f"state-{port}"
    state_directory.mkdir(mode=0o700)
    secret_directory = state_directory / "registry-secrets"
    secret_directory.mkdir(mode=0o700)
    printer_block = ""
    onboarding_block = ""
    if onboarding:
        owner_token = tmp_path / "owner.token"
        owner_token.write_text("o" * 32, encoding="utf-8")
        onboarding_block = f"""

[onboarding]
enabled = true
owner_credential_file = "{owner_token.as_posix()}"
allowed_grove_origins = ["https://grove.example.invalid"]
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
{printer_block}
""".strip(),
        encoding="utf-8",
    )
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize(("control", "expected_control_status"), [(True, 400), (False, 401)])
async def test_serve_starts_api_before_monitors_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: bool,
    expected_control_status: int,
) -> None:
    started = asyncio.Event()
    site_started = asyncio.Event()
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

    monkeypatch.setattr(web, "TCPSite", InstrumentedSite)
    monkeypatch.setattr("klove.app.create_api", capture_application)
    monkeypatch.setattr(app_module, "MoonrakerMonitor", FakeMonitor)
    monkeypatch.setattr(app_module, "MoonrakerOnboardingProbe", FakeProbe)
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(
            write_config(tmp_path, port, printer=True, control=control, onboarding=True),
            stop,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    assert observed_startup_order == [(True, False)]
    await asyncio.sleep(0)
    assert applications[0][ready_key].ready is True
    assert applications[0][owner_authenticator_key].authenticate("o" * 32)
    assert applications[0][owner_sessions_key].active_count == 0

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
