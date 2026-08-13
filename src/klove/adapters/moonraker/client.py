"""Read-only Moonraker WebSocket monitoring client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from klove import __version__
from klove.config import PrinterConfig
from klove.domain.discovery import discover_capabilities
from klove.domain.models import PrinterSnapshot, initial_snapshot
from klove.domain.reducer import (
    apply_status_update,
    establish_snapshot,
    mark_capability_limited,
    mark_disconnected,
    mark_not_ready,
)
from klove.errors import ProtocolError, StateEvidenceError
from klove.registry import PrinterRegistry

LOGGER = logging.getLogger(__name__)
_MAX_MESSAGE_BYTES = 1024 * 1024
_SUBSCRIPTION_FIELDS: dict[str, list[str]] = {
    "display_status": ["message", "progress"],
    "gcode_move": ["extrude_factor", "speed_factor"],
    "heater_bed": ["power", "target", "temperature"],
    "heaters": ["available_heaters", "available_monitors", "available_sensors"],
    "pause_resume": ["is_paused"],
    "print_stats": [
        "filename",
        "filament_used",
        "info",
        "message",
        "print_duration",
        "state",
        "total_duration",
    ],
    "toolhead": ["axis_maximum", "axis_minimum", "extruder", "homed_axes", "position"],
    "virtual_sdcard": ["file_position", "is_active", "progress"],
    "webhooks": ["state", "state_message"],
}


class MoonrakerMonitor:
    """Maintain one conservative view of a configured Moonraker printer."""

    def __init__(
        self,
        config: PrinterConfig,
        api_key: str,
        registry: PrinterRegistry,
        session: aiohttp.ClientSession,
    ) -> None:
        """Bind validated configuration and a secret to one monitor."""
        self._config = config
        self._api_key = api_key
        self._registry = registry
        self._session = session
        self._snapshot = initial_snapshot(config.id)
        self._request_id = 0

    async def run(self, stop: asyncio.Event) -> None:
        """Reconnect with bounded exponential backoff until shutdown."""
        delay = 1.0
        while not stop.is_set():
            try:
                await self.run_once()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError, ProtocolError, StateEvidenceError):
                LOGGER.warning("moonraker monitor disconnected printer=%s", self._config.id)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 30.0)

    async def run_once(self) -> None:
        """Connect, identify, discover, and process messages until disconnect."""
        try:
            async with self._session.ws_connect(
                _websocket_url(self._config.endpoint),
                heartbeat=30,
                max_msg_size=_MAX_MESSAGE_BYTES,
                ssl=self._config.verify_tls,
            ) as websocket:
                await self._identify(websocket)
                await self._bootstrap(websocket)
                while True:
                    message = await _receive_object(websocket)
                    if "method" not in message:
                        raise ProtocolError("unexpected JSON-RPC response")
                    await self._notification(websocket, message)
        finally:
            await self._publish(mark_disconnected(self._snapshot))

    async def _identify(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        await self._rpc(
            websocket,
            "server.connection.identify",
            {
                "api_key": self._api_key,
                "client_name": "Klove",
                "type": "agent",
                "url": "https://github.com/tomlawesome/klove",
                "version": __version__,
            },
        )

    async def _bootstrap(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        server_info = _mapping(await self._rpc(websocket, "server.info"), "server.info")
        if (
            server_info.get("klippy_connected") is not True
            or server_info.get("klippy_state") != "ready"
        ):
            await self._publish(mark_not_ready(self._snapshot, "klippy_not_ready"))
            return
        printer_info = _mapping(await self._rpc(websocket, "printer.info"), "printer.info")
        object_result = _mapping(
            await self._rpc(websocket, "printer.objects.list"), "printer.objects.list"
        )
        if set(object_result) != {"objects"} or not isinstance(object_result["objects"], list):
            raise ProtocolError("printer.objects.list returned an unexpected shape")
        try:
            capabilities = discover_capabilities(object_result["objects"])
        except ValueError as exc:
            raise ProtocolError("printer.objects.list contains invalid object names") from exc
        if not capabilities.dispatch_eligible:
            await self._publish(mark_capability_limited(self._snapshot, capabilities))
            return
        subscription = _mapping(
            await self._rpc(
                websocket,
                "printer.objects.subscribe",
                {"objects": _subscription_for(capabilities.objects)},
            ),
            "printer.objects.subscribe",
        )
        await self._publish(
            establish_snapshot(
                self._snapshot,
                server_info=server_info,
                printer_info=printer_info,
                capabilities=capabilities,
                subscription=subscription,
            )
        )

    async def _rpc(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        self._request_id += 1
        request: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": self._request_id}
        if params is not None:
            request["params"] = dict(params)
        await websocket.send_json(request)
        while True:
            response = await _receive_object(websocket)
            if "method" in response:
                await self._notification(websocket, response)
                continue
            response_id = response.get("id")
            if (
                response.get("jsonrpc") != "2.0"
                or isinstance(response_id, bool)
                or not isinstance(response_id, int)
                or response_id != self._request_id
            ):
                raise ProtocolError("JSON-RPC response does not match its request")
            if "error" in response:
                raise ProtocolError("Moonraker rejected a JSON-RPC request")
            if set(response) != {"jsonrpc", "id", "result"}:
                raise ProtocolError("JSON-RPC response has an unexpected shape")
            return response["result"]

    async def _notification(
        self, websocket: aiohttp.ClientWebSocketResponse, message: Mapping[str, Any]
    ) -> None:
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            raise ProtocolError("JSON-RPC notification is malformed")
        method = message["method"]
        params = message.get("params", [])
        if method == "notify_status_update":
            if not isinstance(params, list) or len(params) != 2:
                raise ProtocolError("status notification is malformed")
            await self._publish(
                apply_status_update(self._snapshot, status_diff=params[0], eventtime=params[1])
            )
        elif method == "notify_klippy_ready":
            if params not in ([], None):
                raise ProtocolError("ready notification is malformed")
            await self._bootstrap(websocket)
        elif method == "notify_klippy_shutdown":
            await self._publish(mark_not_ready(self._snapshot, "klippy_shutdown"))
        elif method == "notify_klippy_disconnected":
            await self._publish(mark_disconnected(self._snapshot, "klippy_disconnected"))

    async def _publish(self, snapshot: PrinterSnapshot) -> None:
        if snapshot.revision <= self._snapshot.revision:
            return
        await self._registry.replace(snapshot)
        self._snapshot = snapshot


def _websocket_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/websocket", "", ""))


def _subscription_for(objects: frozenset[str]) -> dict[str, list[str]]:
    subscriptions = {
        name: fields for name, fields in _SUBSCRIPTION_FIELDS.items() if name in objects
    }
    for name in objects:
        if name.startswith("extruder"):
            subscriptions[name] = ["can_extrude", "power", "target", "temperature"]
        elif name == "fan" or name.startswith(("controller_fan ", "fan_generic ", "heater_fan ")):
            subscriptions[name] = ["rpm", "speed"]
        elif name.startswith("heater_generic "):
            subscriptions[name] = ["power", "target", "temperature"]
        elif name.startswith(("filament_motion_sensor ", "filament_switch_sensor ")):
            subscriptions[name] = ["enabled", "filament_detected"]
    return dict(sorted(subscriptions.items()))


async def _receive_object(websocket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    message = await websocket.receive(timeout=45)
    if message.type is not aiohttp.WSMsgType.TEXT:
        raise ProtocolError("Moonraker WebSocket closed or sent a non-text message")
    try:
        decoded = json.loads(message.data)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProtocolError("Moonraker sent invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("Moonraker JSON-RPC message is not an object")
    return decoded


def _mapping(value: Any, method: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{method} did not return an object")
    return value
