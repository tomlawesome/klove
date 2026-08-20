"""Application composition and lifecycle."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from klove.adapters.moonraker.client import MoonrakerMonitor
from klove.adapters.moonraker.control import MoonrakerControlTransport
from klove.config import load_config, read_secret
from klove.northbound.api import create_api, ready_key
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.control import ControlService, ControlTransport
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator

LOGGER = logging.getLogger(__name__)


async def serve(config_path: Path, stop: asyncio.Event | None = None) -> None:
    """Run the API and all configured Moonraker monitors."""
    config = load_config(config_path)
    stop_event = stop or asyncio.Event()
    registry = PrinterRegistry(printer.id for printer in config.printers)
    admissions = PrinterAdmissionGates()
    timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=50)
    monitor_tasks: list[asyncio.Task[None]] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        transports: dict[str, ControlTransport] = {}
        control_admission_ids: dict[str, str] = {}
        monitors: list[tuple[str, MoonrakerMonitor]] = []
        for printer in config.printers:
            api_key = read_secret(printer.api_key_file)
            monitors.append((printer.id, MoonrakerMonitor(printer, api_key, registry, session)))
            if config.control.enabled and printer.control_enabled:
                transports[printer.id] = MoonrakerControlTransport(
                    printer,
                    api_key,
                    session,
                    request_timeout_seconds=config.control.request_timeout_seconds,
                )
                control_admission_ids[printer.id] = str(printer.uuid)
        controls = ControlService(
            registry,
            transports,
            confirmation_timeout_seconds=config.control.confirmation_timeout_seconds,
            poll_interval_seconds=config.control.poll_interval_seconds,
            idempotency_capacity=config.control.idempotency_capacity,
            admissions=admissions,
            admission_ids=control_admission_ids,
        )
        scopes = {"printers:read"}
        if config.control.enabled:
            scopes.add("printers:control")
        app = create_api(
            registry,
            BearerAuthenticator(read_secret(config.api.token_file), scopes=frozenset(scopes)),
            controls,
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, config.api.listen_host, config.api.listen_port)
        try:
            await site.start()
            app[ready_key].ready = True
            for printer_id, monitor in monitors:
                monitor_tasks.append(
                    asyncio.create_task(monitor.run(stop_event), name=f"moonraker:{printer_id}")
                )
            LOGGER.info("klove ready configured_printers=%d", len(config.printers))
            await stop_event.wait()
        finally:
            app[ready_key].ready = False
            stop_event.set()
            for task in monitor_tasks:
                task.cancel()
            await asyncio.gather(*monitor_tasks, return_exceptions=True)
            await runner.cleanup()
