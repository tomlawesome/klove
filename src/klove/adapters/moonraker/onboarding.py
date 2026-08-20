"""Bounded, address-pinned HTTP probe for canonical Moonraker onboarding."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from klove.domain.discovery import discover_capabilities
from klove.domain.onboarding import MoonrakerEndpoint, PrinterIdentityEvidence
from klove.errors import KloveError

_MAX_CAPABILITY_OBJECTS: Final = 4_096
_MAX_OBJECT_NAME_BYTES: Final = 255
_MAX_API_KEY_BYTES: Final = 4_096
_RESOLVE_FIELDS: Final = {"hostname", "host", "port", "family", "proto", "flags"}
_SERVER_INFO_PATH: Final = "/server/info"
_PRINTER_INFO_PATH: Final = "/printer/info"
_OBJECTS_PATH: Final = "/printer/objects/list"

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
ResolverFactory = Callable[[], AbstractResolver]


class MoonrakerProbeError(KloveError):
    """A direct probe could not produce current, coherent, bounded evidence."""


@dataclass(frozen=True, slots=True, init=False)
class EndpointAddressPolicy:
    """Exact deployment allowlist for direct Moonraker connection addresses."""

    allowed_networks: tuple[IpNetwork, ...]
    max_addresses: int

    def __init__(self, allowed_cidrs: Iterable[str], *, max_addresses: int = 16) -> None:
        if type(max_addresses) is not int or not 1 <= max_addresses <= 64:
            raise ValueError("max_addresses is invalid")
        raw_cidrs = tuple(allowed_cidrs)
        if not raw_cidrs or len(raw_cidrs) > 64:
            raise ValueError("an exact bounded network allowlist is required")
        parsed: list[IpNetwork] = []
        for raw in raw_cidrs:
            if type(raw) is not str or raw != raw.strip() or not raw.isascii():
                raise ValueError("allowed CIDRs must be exact ASCII strings")
            network = ipaddress.ip_network(raw, strict=True)
            if str(network) != raw or network.prefixlen == 0:
                raise ValueError("allowed CIDRs must be canonical and narrower than all-addresses")
            parsed.append(network)
        if len(parsed) != len(set(parsed)):
            raise ValueError("allowed CIDRs must be unique")
        object.__setattr__(self, "allowed_networks", tuple(parsed))
        object.__setattr__(self, "max_addresses", max_addresses)

    def select(self, addresses: Iterable[IpAddress]) -> IpAddress:
        """Validate every DNS answer and choose one deterministic pinned address."""
        unique = frozenset(addresses)
        if not unique or len(unique) > self.max_addresses:
            raise MoonrakerProbeError
        for address in unique:
            if (
                address.is_unspecified
                or address.is_multicast
                or address.is_link_local
                or address.is_reserved
                or not any(
                    address.version == network.version and address in network
                    for network in self.allowed_networks
                )
            ):
                raise MoonrakerProbeError
        return min(unique, key=lambda address: (address.version, int(address)))


@dataclass(frozen=True, slots=True)
class _Resolution:
    hostname: str
    address: IpAddress
    family: socket.AddressFamily
    port: int


@dataclass(frozen=True, slots=True)
class _ServerEvidence:
    moonraker_version: str


@dataclass(frozen=True, slots=True)
class _PrinterEvidence:
    hostname: str
    klipper_version: str


class _PinnedResolver(AbstractResolver):
    def __init__(self, resolution: _Resolution) -> None:
        self._resolution = resolution

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if (
            host != self._resolution.hostname
            or port != self._resolution.port
            or family not in {socket.AF_UNSPEC, self._resolution.family}
        ):
            raise OSError
        return [
            ResolveResult(
                hostname=host,
                host=str(self._resolution.address),
                port=port,
                family=self._resolution.family,
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            )
        ]

    async def close(self) -> None:
        return None


class MoonrakerOnboardingProbe:
    """Authenticate and read one coherent direct identity/capability snapshot."""

    def __init__(  # noqa: PLR0913 -- every probe bound is explicit and independently injectable.
        self,
        address_policy: EndpointAddressPolicy,
        *,
        total_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 3.0,
        max_response_bytes: int = 64 * 1024,
        resolver_factory: ResolverFactory = DefaultResolver,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            isinstance(total_timeout_seconds, bool)
            or not isinstance(total_timeout_seconds, (int, float))
            or not math.isfinite(float(total_timeout_seconds))
            or not 0 < total_timeout_seconds <= 30
        ):
            raise ValueError("total probe timeout is invalid")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not math.isfinite(float(request_timeout_seconds))
            or not 0 < request_timeout_seconds <= total_timeout_seconds
        ):
            raise ValueError("per-request probe timeout is invalid")
        if type(max_response_bytes) is not int or not 1_024 <= max_response_bytes <= 1_048_576:
            raise ValueError("probe response bound is invalid")
        self._address_policy = address_policy
        self._total_timeout = float(total_timeout_seconds)
        self._request_timeout = aiohttp.ClientTimeout(total=float(request_timeout_seconds))
        self._max_response_bytes = max_response_bytes
        self._resolver_factory = resolver_factory
        self._clock_ms = clock_ms or _unix_time_ms

    async def probe(
        self,
        endpoint: MoonrakerEndpoint,
        api_key: str,
    ) -> PrinterIdentityEvidence:
        """Return evidence only when two complete read-only snapshots agree."""
        try:
            _validate_api_key(api_key)
            async with asyncio.timeout(self._total_timeout):
                resolution = await _resolve_endpoint(
                    endpoint,
                    self._address_policy,
                    self._resolver_factory,
                )
                connector = aiohttp.TCPConnector(
                    resolver=_PinnedResolver(resolution),
                    use_dns_cache=False,
                    family=resolution.family,
                    limit=1,
                    limit_per_host=1,
                )
                async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=self._request_timeout,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    auto_decompress=False,
                    trust_env=False,
                ) as session:
                    headers = {"Accept": "application/json", "X-Api-Key": api_key}
                    server_before = _server_evidence(
                        await self._get(session, endpoint, _SERVER_INFO_PATH, headers)
                    )
                    printer_before = _printer_evidence(
                        await self._get(session, endpoint, _PRINTER_INFO_PATH, headers)
                    )
                    objects_before = _capability_objects(
                        await self._get(session, endpoint, _OBJECTS_PATH, headers)
                    )
                    objects_after = _capability_objects(
                        await self._get(session, endpoint, _OBJECTS_PATH, headers)
                    )
                    printer_after = _printer_evidence(
                        await self._get(session, endpoint, _PRINTER_INFO_PATH, headers)
                    )
                    server_after = _server_evidence(
                        await self._get(session, endpoint, _SERVER_INFO_PATH, headers)
                    )
                if (
                    server_before != server_after
                    or printer_before != printer_after
                    or objects_before != objects_after
                ):
                    raise MoonrakerProbeError
                return PrinterIdentityEvidence(
                    server_hostname=str(resolution.address),
                    klipper_hostname=printer_after.hostname,
                    moonraker_version=server_after.moonraker_version,
                    klipper_version=printer_after.klipper_version,
                    capabilities=discover_capabilities(objects_after),
                    observed_at_unix_ms=self._clock_ms(),
                )
        except MoonrakerProbeError:
            raise
        except Exception as exc:
            raise MoonrakerProbeError from exc

    async def _get(
        self,
        session: aiohttp.ClientSession,
        endpoint: MoonrakerEndpoint,
        path: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        async with session.get(
            f"{endpoint.url}{path}",
            headers=headers,
            allow_redirects=False,
            ssl=endpoint.verify_tls,
        ) as response:
            return await _response_result(response, self._max_response_bytes)


async def _resolve_endpoint(
    endpoint: MoonrakerEndpoint,
    policy: EndpointAddressPolicy,
    resolver_factory: ResolverFactory,
) -> _Resolution:
    parsed = urlsplit(endpoint.url)
    hostname = parsed.hostname
    if hostname is None:
        raise MoonrakerProbeError
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        resolver = resolver_factory()
        try:
            results = await resolver.resolve(hostname, port, family=socket.AF_UNSPEC)
        finally:
            await resolver.close()
        addresses = tuple(_decode_resolve_result(result, hostname, port) for result in results)
    else:
        addresses = (literal,)
    selected = policy.select(addresses)
    family = socket.AF_INET6 if selected.version == 6 else socket.AF_INET
    return _Resolution(hostname=hostname, address=selected, family=family, port=port)


def _decode_resolve_result(result: ResolveResult, hostname: str, port: int) -> IpAddress:
    flags = result.get("flags")
    if (
        set(result) != _RESOLVE_FIELDS
        or result["hostname"] != hostname
        or result["port"] != port
        or result["family"] not in {socket.AF_INET, socket.AF_INET6}
        or result["proto"] not in {0, socket.IPPROTO_TCP}
        or isinstance(flags, bool)
        or not isinstance(flags, int)
        or int(flags) not in {0, int(socket.AI_NUMERICHOST | socket.AI_NUMERICSERV)}
        or type(result["host"]) is not str
        or "%" in result["host"]
    ):
        raise MoonrakerProbeError
    address = ipaddress.ip_address(result["host"])
    expected_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    if result["family"] != expected_family or str(address) != result["host"]:
        raise MoonrakerProbeError
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise MoonrakerProbeError
    return address


async def _response_result(response: aiohttp.ClientResponse, maximum_bytes: int) -> dict[str, Any]:
    if response.status != 200:
        raise MoonrakerProbeError
    content_types = response.headers.getall("Content-Type", [])
    content_encodings = response.headers.getall("Content-Encoding", [])
    if (
        len(content_types) != 1
        or response.content_type != "application/json"
        or (response.charset is not None and response.charset.casefold() != "utf-8")
        or content_encodings not in ([], ["identity"])
        or (response.content_length is not None and response.content_length > maximum_bytes)
    ):
        raise MoonrakerProbeError
    body = bytearray()
    async for chunk in response.content.iter_chunked(8_192):
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise MoonrakerProbeError
    document = json.loads(
        body.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(document, dict) or set(document) != {"result"}:
        raise MoonrakerProbeError
    result = document["result"]
    if not isinstance(result, dict):
        raise MoonrakerProbeError
    return result


def _server_evidence(result: dict[str, Any]) -> _ServerEvidence:
    if result.get("klippy_connected") is not True or result.get("klippy_state") != "ready":
        raise MoonrakerProbeError
    return _ServerEvidence(moonraker_version=_bounded_text(result.get("moonraker_version")))


def _printer_evidence(result: dict[str, Any]) -> _PrinterEvidence:
    if result.get("state") != "ready":
        raise MoonrakerProbeError
    return _PrinterEvidence(
        hostname=_bounded_text(result.get("hostname")),
        klipper_version=_bounded_text(result.get("software_version")),
    )


def _capability_objects(result: dict[str, Any]) -> tuple[str, ...]:
    if set(result) != {"objects"} or not isinstance(result["objects"], list):
        raise MoonrakerProbeError
    supplied = result["objects"]
    if len(supplied) > _MAX_CAPABILITY_OBJECTS:
        raise MoonrakerProbeError
    objects = tuple(
        _bounded_text(value, maximum_bytes=_MAX_OBJECT_NAME_BYTES) for value in supplied
    )
    if len(objects) != len(set(objects)):
        raise MoonrakerProbeError
    return tuple(sorted(objects))


def _bounded_text(value: object, *, maximum_bytes: int = 255) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MoonrakerProbeError
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) > maximum_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise MoonrakerProbeError
    return value


def _validate_api_key(value: str) -> None:
    if type(value) is not str:
        raise MoonrakerProbeError
    encoded = value.encode("ascii", errors="strict")
    if not 32 <= len(encoded) <= _MAX_API_KEY_BYTES or any(
        ord(character) < 33 or ord(character) == 127 for character in value
    ):
        raise MoonrakerProbeError


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object member", key, 0)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise json.JSONDecodeError("non-finite JSON number", value, 0)


def _unix_time_ms() -> int:
    return time.time_ns() // 1_000_000
