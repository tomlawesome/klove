from __future__ import annotations

import ipaddress
import json
import socket
import time
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestServer

from klove.adapters.moonraker.onboarding import (
    EndpointAddressPolicy,
    MoonrakerOnboardingProbe,
    MoonrakerProbeError,
    _bounded_text,
    _capability_objects,
    _decode_resolve_result,
    _PinnedResolver,
    _printer_evidence,
    _Resolution,
    _resolve_endpoint,
    _server_evidence,
    _unix_time_ms,
)
from klove.domain.onboarding import MoonrakerEndpoint, PrinterIdentityEvidence

API_KEY = "m" * 32
OBJECTS = ["pause_resume", "print_stats", "virtual_sdcard", "extruder"]


class FakeResolver(AbstractResolver):
    def __init__(self, results: list[ResolveResult] | Exception) -> None:
        self.results = results
        self.calls: list[tuple[str, int, socket.AddressFamily]] = []
        self.closed = False

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        self.calls.append((host, port, family))
        if isinstance(self.results, Exception):
            raise self.results
        return self.results

    async def close(self) -> None:
        self.closed = True


def resolve_result(  # noqa: PLR0913 -- each resolver field is independently tested.
    host: str = "127.0.0.1",
    *,
    hostname: str = "moonraker.test",
    port: int = 7125,
    family: socket.AddressFamily = socket.AF_INET,
    proto: int = socket.IPPROTO_TCP,
    flags: int = 0,
) -> ResolveResult:
    return ResolveResult(
        hostname=hostname,
        host=host,
        port=port,
        family=family,
        proto=proto,
        flags=flags,
    )


def server_info(version: str = "v0.9.3") -> dict[str, object]:
    return {
        "klippy_connected": True,
        "klippy_state": "ready",
        "moonraker_version": version,
        "warnings": [],
    }


def printer_info(hostname: str = "klipper") -> dict[str, object]:
    return {
        "state": "ready",
        "hostname": hostname,
        "software_version": "v0.12.0",
        "state_message": "ready",
    }


async def run_probe(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    *,
    max_response_bytes: int = 64 * 1024,
) -> tuple[PrinterIdentityEvidence, list[web.Request]]:
    requests: list[web.Request] = []

    async def recorded(request: web.Request) -> web.StreamResponse:
        requests.append(request)
        return await handler(request)

    app = web.Application()
    app.router.add_get("/{tail:.*}", recorded)
    async with TestServer(app) as server:
        endpoint = MoonrakerEndpoint(url=str(server.make_url("")).rstrip("/"), verify_tls=False)
        probe = MoonrakerOnboardingProbe(
            EndpointAddressPolicy(("127.0.0.0/8",)),
            max_response_bytes=max_response_bytes,
            clock_ms=lambda: 900,
        )
        result = await probe.probe(endpoint, API_KEY)
    return result, requests


@pytest.mark.asyncio
async def test_probe_pins_authenticates_and_requires_two_coherent_snapshots() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path == "/server/info":
            result = server_info()
        elif request.path == "/printer/info":
            result = printer_info()
        else:
            assert request.path == "/printer/objects/list"
            result = {"objects": OBJECTS}
        return web.json_response({"result": result})

    evidence, requests = await run_probe(handler)

    assert evidence.server_hostname == "127.0.0.1"
    assert evidence.klipper_hostname == "klipper"
    assert evidence.capabilities.dispatch_eligible
    assert evidence.observed_at_unix_ms == 900
    assert [request.path for request in requests] == [
        "/server/info",
        "/printer/info",
        "/printer/objects/list",
        "/printer/objects/list",
        "/printer/info",
        "/server/info",
    ]
    assert all(request.headers["X-Api-Key"] == API_KEY for request in requests)
    assert all(request.query_string == "" for request in requests)


@pytest.mark.asyncio
async def test_probe_uses_the_exact_dns_answer_for_host_and_connection() -> None:
    hosts: list[str] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        hosts.append(request.headers["Host"])
        result = (
            server_info()
            if request.path == "/server/info"
            else printer_info()
            if request.path == "/printer/info"
            else {"objects": OBJECTS}
        )
        return web.json_response({"result": result})

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    async with TestServer(app) as server:
        port = server.port
        assert port is not None
        resolver = FakeResolver([resolve_result(port=port)])
        endpoint = MoonrakerEndpoint(
            url=f"http://moonraker.test:{port}",
            allow_insecure_http=True,
            verify_tls=False,
        )
        probe = MoonrakerOnboardingProbe(
            EndpointAddressPolicy(("127.0.0.0/8",)),
            resolver_factory=lambda: resolver,
            clock_ms=lambda: 900,
        )
        evidence = await probe.probe(endpoint, API_KEY)

    assert evidence.server_hostname == "127.0.0.1"
    assert resolver.calls == [("moonraker.test", port, socket.AF_UNSPEC)]
    assert resolver.closed
    assert hosts == [f"moonraker.test:{port}"] * 6


@pytest.mark.parametrize(
    ("cidrs", "maximum"),
    [
        ((), 16),
        (("0.0.0.0/0",), 16),
        (("10.0.0.1/8",), 16),
        (("10.0.0.0/8", "10.0.0.0/8"), 16),
        ((" 10.0.0.0/8",), 16),
        (("2001:DB8::/32",), 16),
        (("10.0.0.0/8",), 0),
        (("10.0.0.0/8",), True),
    ],
)
def test_address_policy_rejects_ambiguous_or_unbounded_configuration(
    cidrs: tuple[str, ...], maximum: int
) -> None:
    with pytest.raises(ValueError):
        EndpointAddressPolicy(cidrs, max_addresses=maximum)


def test_address_policy_rejects_any_mixed_or_unsafe_answer_and_selects_canonically() -> None:
    policy = EndpointAddressPolicy(("10.0.0.0/8", "127.0.0.0/8"))
    assert policy.select(
        (ipaddress.ip_address("127.0.0.2"), ipaddress.ip_address("10.0.0.2"))
    ) == ipaddress.ip_address("10.0.0.2")

    for addresses in (
        (),
        (ipaddress.ip_address("10.0.0.2"), ipaddress.ip_address("192.168.1.2")),
        (ipaddress.ip_address("169.254.169.254"),),
        (ipaddress.ip_address("224.0.0.1"),),
        (ipaddress.ip_address("0.0.0.0"),),  # noqa: S104 -- explicit unsafe test case.
        (ipaddress.ip_address("240.0.0.1"),),
    ):
        with pytest.raises(MoonrakerProbeError):
            policy.select(addresses)

    with pytest.raises(MoonrakerProbeError):
        EndpointAddressPolicy(("10.0.0.0/8",), max_addresses=1).select(
            (ipaddress.ip_address("10.0.0.1"), ipaddress.ip_address("10.0.0.2"))
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"hostname": "other.test"},
        {"port": 7126},
        {"family": socket.AF_UNIX},
        {"proto": socket.IPPROTO_UDP},
        {"flags": "invalid"},
        {"host": "127.0.0.01"},
        {"host": "::ffff:127.0.0.1", "family": socket.AF_INET6},
        {"host": "127.0.0.1", "family": socket.AF_INET6},
    ],
)
def test_resolver_results_must_be_exact_numeric_tcp_answers(updates: dict[str, object]) -> None:
    result: dict[str, object] = dict(resolve_result())
    result.update(updates)
    with pytest.raises((MoonrakerProbeError, ValueError)):
        _decode_resolve_result(result, "moonraker.test", 7125)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_pinned_resolver_refuses_every_other_connection_target() -> None:
    resolution = _Resolution(
        hostname="moonraker.test",
        address=ipaddress.ip_address("10.0.0.2"),
        family=socket.AF_INET,
        port=7125,
    )
    resolver = _PinnedResolver(resolution)
    result = await resolver.resolve("moonraker.test", 7125, socket.AF_UNSPEC)
    assert result[0]["host"] == "10.0.0.2"
    await resolver.close()
    for host, port, family in (
        ("other.test", 7125, socket.AF_INET),
        ("moonraker.test", 7126, socket.AF_INET),
        ("moonraker.test", 7125, socket.AF_INET6),
    ):
        with pytest.raises(OSError):
            await resolver.resolve(host, port, family)


@pytest.mark.asyncio
async def test_resolution_closes_resolver_and_rejects_disallowed_dns_answers() -> None:
    resolver = FakeResolver(
        [
            resolve_result(host="127.0.0.1"),
            resolve_result(host="169.254.169.254"),
        ]
    )
    endpoint = MoonrakerEndpoint(
        url="http://moonraker.test:7125",
        allow_insecure_http=True,
        verify_tls=False,
    )
    with pytest.raises(MoonrakerProbeError):
        await _resolve_endpoint(
            endpoint,
            EndpointAddressPolicy(("127.0.0.0/8",)),
            lambda: resolver,
        )
    assert resolver.closed


@pytest.mark.asyncio
async def test_resolution_fails_closed_if_a_caller_bypasses_endpoint_validation() -> None:
    endpoint = MoonrakerEndpoint.model_construct(
        url="http://",
        allow_insecure_http=False,
        verify_tls=False,
    )
    with pytest.raises(MoonrakerProbeError):
        await _resolve_endpoint(
            endpoint,
            EndpointAddressPolicy(("127.0.0.0/8",)),
            lambda: FakeResolver([]),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_timeout_seconds": 0},
        {"total_timeout_seconds": float("nan")},
        {"total_timeout_seconds": True},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": 11},
        {"request_timeout_seconds": False},
        {"max_response_bytes": 100},
        {"max_response_bytes": True},
    ],
)
def test_probe_limits_are_finite_and_bounded(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MoonrakerOnboardingProbe(EndpointAddressPolicy(("127.0.0.0/8",)), **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_key",
    ["short", "m" * 4097, "has space" * 8, "é" * 32, b"m" * 32],
)
async def test_probe_rejects_invalid_credentials_before_network(api_key: object) -> None:
    endpoint = MoonrakerEndpoint(url="http://127.0.0.1:7125", verify_tls=False)
    probe = MoonrakerOnboardingProbe(EndpointAddressPolicy(("127.0.0.0/8",)))
    with pytest.raises(MoonrakerProbeError):
        await probe.probe(endpoint, cast(str, api_key))


def test_strict_evidence_decoders_reject_missing_stale_and_ambiguous_values() -> None:
    assert _server_evidence(server_info()).moonraker_version == "v0.9.3"
    assert _printer_evidence(printer_info()).hostname == "klipper"
    assert _capability_objects({"objects": list(reversed(OBJECTS))}) == tuple(sorted(OBJECTS))
    assert _bounded_text("exact") == "exact"

    invalid_server: tuple[dict[str, object], ...] = (
        {},
        {**server_info(), "klippy_connected": False},
        {**server_info(), "klippy_state": "startup"},
        {**server_info(), "moonraker_version": " version"},
    )
    invalid_printer: tuple[dict[str, object], ...] = (
        {},
        {**printer_info(), "state": "shutdown"},
        {**printer_info(), "hostname": "host\nname"},
        {**printer_info(), "software_version": None},
    )
    invalid_objects: tuple[dict[str, object], ...] = (
        {},
        {"objects": "print_stats"},
        {"objects": ["print_stats", "print_stats"]},
        {"objects": [" object"]},
        {"objects": ["x" * 256]},
        {"objects": [f"object-{index}" for index in range(4_097)]},
    )
    for value in invalid_server:
        with pytest.raises(MoonrakerProbeError):
            _server_evidence(value)
    for value in invalid_printer:
        with pytest.raises(MoonrakerProbeError):
            _printer_evidence(value)
    for value in invalid_objects:
        with pytest.raises(MoonrakerProbeError):
            _capability_objects(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift_path", ["/server/info", "/printer/info", "/printer/objects/list"])
async def test_probe_rejects_identity_or_capability_drift(drift_path: str) -> None:
    calls: dict[str, int] = {}

    async def handler(request: web.Request) -> web.StreamResponse:
        calls[request.path] = calls.get(request.path, 0) + 1
        changed = request.path == drift_path and calls[request.path] == 2
        if request.path == "/server/info":
            result = server_info("v0.9.4" if changed else "v0.9.3")
        elif request.path == "/printer/info":
            result = printer_info("replacement" if changed else "klipper")
        else:
            result = {"objects": [*OBJECTS, "fan"] if changed else OBJECTS}
        return web.json_response({"result": result})

    with pytest.raises(MoonrakerProbeError):
        await run_probe(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content_type", "body", "headers"),
    [
        (401, "application/json", b'{"error":"denied"}', {}),
        (200, "text/plain", b'{"result":{}}', {}),
        (200, "application/json", b'{"result":{},"extra":1}', {}),
        (200, "application/json", b'{"result":[]}', {}),
        (200, "application/json", b'{"result":{"x":1,"x":2}}', {}),
        (200, "application/json", b'{"result":{"x":NaN}}', {}),
        (200, "application/json", b"\xff", {}),
        (200, "application/json; charset=latin-1", b'{"result":{}}', {}),
        (200, "application/json", b'{"result":{}}', {"Content-Encoding": "gzip"}),
    ],
)
async def test_probe_rejects_malformed_http_and_json_boundaries(
    status: int,
    content_type: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    async def handler(_request: web.Request) -> web.StreamResponse:
        return web.Response(
            status=status,
            body=body,
            content_type=None,
            headers={
                "Content-Type": content_type,
                **headers,
            },
        )

    with pytest.raises(MoonrakerProbeError):
        await run_probe(handler)


@pytest.mark.asyncio
async def test_probe_rejects_streams_larger_than_the_body_limit() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b'{"result":{"padding":"' + b"x" * 2_000)
        await response.write_eof(b'"}}')
        return response

    with pytest.raises(MoonrakerProbeError):
        await run_probe(handler, max_response_bytes=1_024)


def test_response_examples_remain_plain_json_without_secret_material() -> None:
    document = json.dumps({"result": server_info()}, sort_keys=True)
    assert API_KEY not in document
    assert "moonraker_version" in document


def test_default_probe_clock_returns_epoch_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time_ns", lambda: 1_234_567_890)
    assert _unix_time_ms() == 1_234
