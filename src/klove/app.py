"""Application composition and lifecycle."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from klove.adapters.moonraker.client import MoonrakerMonitor
from klove.config import load_config, read_secret
from klove.northbound.api import create_api, ready_key
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator

LOGGER = logging.getLogger(__name__)


async def serve(config_path: Path, stop: asyncio.Event | None = None) -> None:
    """Run the read-only API and all configured Moonraker monitors."""
    config = load_config(config_path)
    stop_event = stop or asyncio.Event()
    registry = PrinterRegistry(printer.id for printer in config.printers)
    api_token = read_secret(config.api.token_file)
    app = create_api(registry, BearerAuthenticator(api_token))
    runner = web.AppRunner(app, access_log=None)
    timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=50)
    monitor_tasks: list[asyncio.Task[None]] = []
    await runner.setup()
    site = web.TCPSite(runner, config.api.listen_host, config.api.listen_port)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for printer in config.printers:
                monitor = MoonrakerMonitor(
                    printer,
                    read_secret(printer.api_key_file),
                    registry,
                    session,
                )
                monitor_tasks.append(
                    asyncio.create_task(monitor.run(stop_event), name=f"moonraker:{printer.id}")
                )
            await site.start()
            app[ready_key].ready = True
            LOGGER.info("klove ready configured_printers=%d", len(config.printers))
            await stop_event.wait()
    finally:
        app[ready_key].ready = False
        stop_event.set()
        for task in monitor_tasks:
            task.cancel()
        await asyncio.gather(*monitor_tasks, return_exceptions=True)
        await runner.cleanup()
