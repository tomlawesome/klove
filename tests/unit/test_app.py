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
from klove.northbound.api import create_api, ready_key


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def write_config(tmp_path: Path, port: int, *, printer: bool, control: bool = False) -> Path:
    api_token = tmp_path / "api.token"
    api_token.write_text("a" * 32, encoding="utf-8")
    printer_block = ""
    if printer:
        moonraker_token = tmp_path / "moonraker.token"
        moonraker_token.write_text("m" * 32, encoding="utf-8")
        printer_block = f"""

[[printers]]
id = "voron"
uuid = "11111111-1111-4111-8111-111111111111"
endpoint = "http://127.0.0.1:7125"
api_key_file = "{moonraker_token.as_posix()}"
control_enabled = {str(control).lower()}
"""
    config = tmp_path / f"config-{port}.toml"
    config.write_text(
        f"""
[api]
listen_host = "127.0.0.1"
listen_port = {port}
token_file = "{api_token.as_posix()}"

[control]
enabled = {str(control).lower()}
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

    monkeypatch.setattr(web, "TCPSite", InstrumentedSite)
    monkeypatch.setattr("klove.app.create_api", capture_application)
    monkeypatch.setattr(app_module, "MoonrakerMonitor", FakeMonitor)
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(write_config(tmp_path, port, printer=True, control=control), stop)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    assert observed_startup_order == [(True, True)]

    async with aiohttp.ClientSession() as session:
        response = await session.get(f"http://127.0.0.1:{port}/health/ready")
        assert response.status == 200
        assert await response.json() == {"status": "ready"}
        control_response = await session.post(
            f"http://127.0.0.1:{port}/v1/printers/voron/commands/pause",
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
