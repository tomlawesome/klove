from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import aiohttp
import pytest

import klove.app as app_module
from klove.app import serve


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
async def test_serve_starts_read_only_api_and_cleans_up_monitors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    class FakeMonitor:
        def __init__(self, *_args: Any) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            started.set()
            await stop.wait()

    monkeypatch.setattr(app_module, "MoonrakerMonitor", FakeMonitor)
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(write_config(tmp_path, port, printer=True, control=True), stop)
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    async with aiohttp.ClientSession() as session:
        response = await session.get(f"http://127.0.0.1:{port}/health/ready")
        assert response.status == 200
        assert await response.json() == {"status": "ready"}
        control_response = await session.post(
            f"http://127.0.0.1:{port}/v1/printers/voron/commands/pause",
            headers={"Authorization": f"Bearer {'a' * 32}"},
        )
        assert control_response.status == 400

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
