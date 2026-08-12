from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest

from klove.adapters.moonraker.client import (
    MoonrakerMonitor,
    _mapping,
    _receive_object,
    _subscription_for,
    _websocket_url,
)
from klove.config import PrinterConfig
from klove.domain.models import initial_snapshot
from klove.errors import ProtocolError
from klove.registry import PrinterRegistry


class FakeWebSocket:
    def __init__(self, *messages: aiohttp.WSMessage) -> None:
        self.messages = deque(messages)
        self.sent: list[dict[str, Any]] = []

    async def receive(self, *, timeout: float) -> aiohttp.WSMessage:
        assert timeout == 45
        return self.messages.popleft()

    async def send_json(self, value: dict[str, Any]) -> None:
        self.sent.append(value)


def text_message(value: object) -> aiohttp.WSMessage:
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(value), "")


def monitor() -> MoonrakerMonitor:
    return MoonrakerMonitor(
        PrinterConfig(
            id="voron",
            endpoint="http://127.0.0.1:7125",
            api_key_file=Path("unused"),
        ),
        "a" * 32,
        PrinterRegistry(["voron"]),
        cast(Any, object()),
    )


def test_websocket_url_and_subscriptions_are_deterministic() -> None:
    assert _websocket_url("https://printer.example.invalid:7125") == (
        "wss://printer.example.invalid:7125/websocket"
    )
    assert _websocket_url("http://127.0.0.1:7125") == "ws://127.0.0.1:7125/websocket"
    assert _subscription_for(frozenset({"print_stats", "mystery"})) == {
        "print_stats": [
            "filename",
            "filament_used",
            "info",
            "message",
            "print_duration",
            "state",
            "total_duration",
        ]
    }


@pytest.mark.asyncio
async def test_receive_object_accepts_only_text_json_objects() -> None:
    assert await _receive_object(cast(Any, FakeWebSocket(text_message({"ok": True})))) == {
        "ok": True
    }

    invalid_messages = [
        aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, b"{}", ""),
        aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, "not-json", ""),
        text_message([]),
    ]
    for message in invalid_messages:
        with pytest.raises(ProtocolError):
            await _receive_object(cast(Any, FakeWebSocket(message)))


def test_mapping_rejects_non_objects() -> None:
    assert _mapping({"ok": True}, "test") == {"ok": True}
    with pytest.raises(ProtocolError, match="test did not return an object"):
        _mapping([], "test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"jsonrpc": "2.0", "id": 999, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -1}},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "extra": True},
    ],
)
async def test_rpc_rejects_mismatched_error_and_extra_responses(
    response: dict[str, Any],
) -> None:
    client = monitor()
    websocket = FakeWebSocket(text_message(response))
    with pytest.raises(ProtocolError):
        await client._rpc(cast(Any, websocket), "server.info")
    assert websocket.sent[0] == {"jsonrpc": "2.0", "method": "server.info", "id": 1}


@pytest.mark.asyncio
async def test_rpc_can_ignore_unrelated_notification_before_matching_response() -> None:
    client = monitor()
    websocket = FakeWebSocket(
        text_message({"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": []}),
        text_message({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}),
    )
    assert await client._rpc(cast(Any, websocket), "server.info", {"x": 1}) == {"ok": True}


@pytest.mark.asyncio
async def test_notification_envelopes_fail_closed() -> None:
    client = monitor()
    websocket = cast(Any, FakeWebSocket())
    for message in (
        {},
        {"jsonrpc": "2.0", "method": 1},
        {"jsonrpc": "2.0", "method": "notify_status_update", "params": []},
        {"jsonrpc": "2.0", "method": "notify_klippy_ready", "params": ["unexpected"]},
    ):
        with pytest.raises(ProtocolError):
            await client._notification(websocket, message)


@pytest.mark.asyncio
async def test_publish_ignores_a_nonadvancing_local_snapshot() -> None:
    client = monitor()
    await client._publish(initial_snapshot("voron"))
    assert (await client._registry.get("voron")) == initial_snapshot("voron")


@pytest.mark.asyncio
async def test_monitor_loop_retries_bounded_errors_and_stops_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = monitor()
    stop = asyncio.Event()
    calls = 0

    async def run_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProtocolError("bounded")
        stop.set()

    async def immediate_timeout(awaitable: Any, *, timeout: float) -> None:
        awaitable.close()
        assert timeout == 1
        raise TimeoutError

    monkeypatch.setattr(client, "run_once", run_once)
    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)
    await client.run(stop)
    assert calls == 2


@pytest.mark.asyncio
async def test_monitor_loop_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = monitor()

    async def cancelled() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(client, "run_once", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await client.run(asyncio.Event())


@pytest.mark.asyncio
async def test_monitor_loop_does_nothing_after_stop() -> None:
    stop = asyncio.Event()
    stop.set()
    await monitor().run(stop)
