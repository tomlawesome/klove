from __future__ import annotations

import asyncio
from typing import cast

import aiohttp
import pytest

from klove.adapters.moonraker.client import MoonrakerMonitorConfig
from klove.domain.onboarding import MoonrakerEndpoint, PrinterLifecycle, RegisteredPrinter
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.control import ControlService, ControlTransport
from klove.orchestration.runtime_registry import RegistryRuntimeError, RegistryRuntimeSupervisor
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore
from klove.registry import PrinterRegistry

from ..onboarding_helpers import (
    COMPATIBILITY_REF,
    MOONRAKER_REF,
    OTHER_COMPATIBILITY_REF,
    OTHER_MOONRAKER_REF,
    OTHER_PRINTER_UUID,
    PRINTER_UUID,
    printer,
    safety_profile,
)


class FakeStore:
    def __init__(self, records: tuple[object, ...]) -> None:
        self.records = records
        self.failure: Exception | None = None
        self.get_failure: Exception | None = None
        self.calls: list[bool] = []

    def list(self, *, include_removed: bool = False) -> tuple[RegisteredPrinter, ...]:
        self.calls.append(include_removed)
        if self.failure is not None:
            raise self.failure
        return cast(tuple[RegisteredPrinter, ...], self.records)

    def get(self, printer_uuid: str) -> RegisteredPrinter | None:
        if self.get_failure is not None:
            raise self.get_failure
        if self.failure is not None:
            raise self.failure
        matches = [
            record
            for record in self.records
            if type(record) is RegisteredPrinter and record.printer_uuid == printer_uuid
        ]
        return matches[0] if len(matches) == 1 else None


class FakeSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[tuple[str, int]] = []

    def read(self, reference: str, *, minimum_length: int) -> str:
        self.reads.append((reference, minimum_length))
        value = self.values[reference]
        if len(value) < minimum_length:
            raise ValueError
        return value


class FakeMonitor:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        self.started.set()
        if self.failure is not None:
            raise self.failure
        try:
            await stop.wait()
        finally:
            self.stopped.set()


class RecordingFactory:
    def __init__(
        self,
        failures: dict[str, Exception] | None = None,
        constructor_failure: str | None = None,
    ) -> None:
        self.failures = failures or {}
        self.constructor_failure = constructor_failure
        self.calls: list[tuple[MoonrakerMonitorConfig, str]] = []
        self.monitors: list[FakeMonitor] = []

    def __call__(
        self,
        config: MoonrakerMonitorConfig,
        api_key: str,
        _registry: PrinterRegistry,
        _session: aiohttp.ClientSession,
    ) -> FakeMonitor:
        self.calls.append((config, api_key))
        if config.id == self.constructor_failure:
            raise RuntimeError
        monitor = FakeMonitor(failure=self.failures.get(config.id))
        self.monitors.append(monitor)
        return monitor


class BlockingRegistry(PrinterRegistry):
    def __init__(self) -> None:
        super().__init__([])
        self.block_register = False
        self.block_unregister = False
        self.register_entered = asyncio.Event()
        self.unregister_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def register(self, printer_id: str) -> None:
        if self.block_register:
            self.register_entered.set()
            await self.release.wait()
        await super().register(printer_id)

    async def unregister(self, printer_id: str) -> bool:
        if self.block_unregister:
            self.unregister_entered.set()
            await self.release.wait()
        return await super().unregister(printer_id)


def other_printer(**updates: object) -> RegisteredPrinter:
    values: dict[str, object] = {
        "printer_uuid": OTHER_PRINTER_UUID,
        "display_name": "Second printer",
        "endpoint": MoonrakerEndpoint(url="http://127.0.0.1:7126", verify_tls=False),
        "moonraker_credential_ref": OTHER_MOONRAKER_REF,
        "compatibility_credential_ref": OTHER_COMPATIBILITY_REF,
        "safety_profiles": (safety_profile(printer_uuid=OTHER_PRINTER_UUID),),
    }
    values.update(updates)
    return printer(**values)


def secret_values() -> dict[str, str]:
    return {
        MOONRAKER_REF: "m" * 32,
        COMPATIBILITY_REF: "c" * 20,
        OTHER_MOONRAKER_REF: "n" * 32,
        OTHER_COMPATIBILITY_REF: "d" * 20,
    }


def supervisor(
    store: FakeStore,
    secrets: FakeSecrets,
    registry: PrinterRegistry,
    factory: RecordingFactory,
    admissions: PrinterAdmissionGates | None = None,
) -> RegistryRuntimeSupervisor:
    return RegistryRuntimeSupervisor(
        cast(PrinterStore, store),
        cast(SecretStore, secrets),
        registry,
        cast(aiohttp.ClientSession, object()),
        admissions=admissions or PrinterAdmissionGates(),
        monitor_factory=factory,
    )


def control_service(
    registry: PrinterRegistry,
    admissions: PrinterAdmissionGates,
) -> ControlService:
    return ControlService(
        registry,
        {},
        confirmation_timeout_seconds=1,
        poll_interval_seconds=0.1,
        idempotency_capacity=10,
        admissions=admissions,
    )


@pytest.mark.parametrize("with_controls", [True, False])
def test_runtime_requires_control_routes_and_factory_together(with_controls: bool) -> None:
    registry = PrinterRegistry([])
    admissions = PrinterAdmissionGates()
    controls = control_service(registry, admissions) if with_controls else None
    factory = (
        (lambda _record, _key, _session: cast(ControlTransport, object()))
        if not with_controls
        else None
    )

    with pytest.raises(ValueError):
        RegistryRuntimeSupervisor(
            cast(PrinterStore, FakeStore(())),
            cast(SecretStore, FakeSecrets({})),
            registry,
            cast(aiohttp.ClientSession, object()),
            admissions=admissions,
            controls=controls,
            control_transport_factory=factory,
        )


@pytest.mark.asyncio
async def test_runtime_installs_and_removes_control_route_with_monitor() -> None:
    admissions = PrinterAdmissionGates()
    registry = PrinterRegistry([])
    controls = control_service(registry, admissions)
    transport = cast(ControlTransport, object())
    store = FakeStore((printer(),))
    runtime = RegistryRuntimeSupervisor(
        cast(PrinterStore, store),
        cast(SecretStore, FakeSecrets(secret_values())),
        registry,
        cast(aiohttp.ClientSession, object()),
        admissions=admissions,
        monitor_factory=RecordingFactory(),
        controls=controls,
        control_transport_factory=lambda _record, _key, _session: transport,
    )

    assert await runtime.refresh() == (PRINTER_UUID,)
    with pytest.raises(KeyError):
        controls.register_transport(PRINTER_UUID, transport)

    store.records = ()
    assert await runtime.refresh() == ()
    assert controls.unregister_transport(PRINTER_UUID) is False
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_replacement_waits_for_the_shared_printer_gate() -> None:
    admissions = PrinterAdmissionGates()
    registry = PrinterRegistry([])
    factory = RecordingFactory()
    runtime = supervisor(
        FakeStore((printer(),)),
        FakeSecrets(secret_values()),
        registry,
        factory,
        admissions,
    )

    async with admissions.hold(PRINTER_UUID):
        task = asyncio.create_task(runtime.refresh())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert factory.calls == []
        assert await registry.get(PRINTER_UUID) is None
    assert await task == (PRINTER_UUID,)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_refresh_deactivates_when_current_record_cannot_be_reproved() -> None:
    record = printer()
    store = FakeStore((record,))
    registry = PrinterRegistry([])
    runtime = supervisor(store, FakeSecrets(secret_values()), registry, RecordingFactory())

    assert await runtime.refresh() == (PRINTER_UUID,)
    store.get_failure = RuntimeError()

    assert await runtime.refresh() == ()
    assert await registry.get(PRINTER_UUID) is None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_refresh_activates_only_complete_active_records_and_removes_missing() -> None:
    active = printer()
    disabled = other_printer(
        lifecycle=PrinterLifecycle.DISABLED,
        control_enabled=False,
        dispatch_enabled=False,
    )
    removed = other_printer(
        printer_uuid="33333333-3333-4333-8333-333333333333",
        endpoint=MoonrakerEndpoint(url="http://127.0.0.1:7127", verify_tls=False),
        lifecycle=PrinterLifecycle.REMOVED,
        moonraker_credential_ref=None,
        compatibility_credential_ref=None,
        safety_profiles=(),
        control_enabled=False,
        dispatch_enabled=False,
    )
    store = FakeStore((active, disabled, removed, object()))
    secrets = FakeSecrets(secret_values())
    registry = PrinterRegistry([])
    factory = RecordingFactory()
    runtime = supervisor(store, secrets, registry, factory)

    assert await runtime.refresh() == (PRINTER_UUID,)
    await asyncio.sleep(0)
    assert store.calls == [True]
    assert [call[0].id for call in factory.calls] == [PRINTER_UUID]
    assert factory.calls[0][0].endpoint == active.endpoint.url
    assert factory.calls[0][0].verify_tls is False
    assert factory.calls[0][1] == "m" * 32
    assert await registry.get(PRINTER_UUID) is not None
    assert await registry.get(OTHER_PRINTER_UUID) is None

    secrets.values.pop(MOONRAKER_REF)
    assert await runtime.refresh() == ()
    assert factory.monitors[0].stopped.is_set()
    assert await registry.get(PRINTER_UUID) is None

    secrets.values[MOONRAKER_REF] = "m" * 32
    assert await runtime.refresh() == (PRINTER_UUID,)
    await asyncio.sleep(0)
    store.records = ()
    assert await runtime.refresh() == ()
    assert factory.monitors[1].stopped.is_set()
    assert await registry.get(PRINTER_UUID) is None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_refresh_replaces_changed_record_but_preserves_an_exact_match() -> None:
    original = printer()
    store = FakeStore((original,))
    secrets = FakeSecrets(secret_values())
    registry = PrinterRegistry([])
    factory = RecordingFactory()
    runtime = supervisor(store, secrets, registry, factory)

    assert await runtime.refresh() == (PRINTER_UUID,)
    await asyncio.sleep(0)
    first = factory.monitors[0]
    assert await runtime.refresh() == (PRINTER_UUID,)
    assert len(factory.monitors) == 1

    replacement = printer(
        endpoint=MoonrakerEndpoint(url="http://127.0.0.1:7130", verify_tls=False),
        revision=2,
        updated_at_unix_ms=1_100,
    )
    store.records = (replacement,)
    assert await runtime.refresh() == (PRINTER_UUID,)
    await asyncio.sleep(0)
    assert first.stopped.is_set()
    assert len(factory.monitors) == 2
    assert factory.calls[-1][0].endpoint == replacement.endpoint.url

    secrets.values[MOONRAKER_REF] = "x" * 32
    assert await runtime.refresh() == (PRINTER_UUID,)
    assert len(factory.monitors) == 3
    assert factory.calls[-1][1] == "x" * 32
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_disable_and_removal_each_stop_an_active_route() -> None:
    active = printer()
    store = FakeStore((active,))
    registry = PrinterRegistry([])
    factory = RecordingFactory()
    runtime = supervisor(store, FakeSecrets(secret_values()), registry, factory)
    assert await runtime.refresh() == (PRINTER_UUID,)
    await asyncio.sleep(0)

    store.records = (
        printer(
            lifecycle=PrinterLifecycle.DISABLED,
            control_enabled=False,
            dispatch_enabled=False,
            revision=2,
            updated_at_unix_ms=1_100,
        ),
    )
    assert await runtime.refresh() == ()
    assert factory.monitors[0].stopped.is_set()

    store.records = (printer(revision=3, updated_at_unix_ms=1_200),)
    assert await runtime.refresh() == (PRINTER_UUID,)
    await asyncio.sleep(0)
    store.records = (
        printer(
            lifecycle=PrinterLifecycle.REMOVED,
            moonraker_credential_ref=None,
            compatibility_credential_ref=None,
            safety_profiles=(),
            control_enabled=False,
            dispatch_enabled=False,
            revision=4,
            updated_at_unix_ms=1_300,
        ),
    )
    assert await runtime.refresh() == ()
    assert factory.monitors[1].stopped.is_set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_refresh_removes_unmanaged_and_colliding_registry_routes() -> None:
    admissions = PrinterAdmissionGates()
    registry = PrinterRegistry(["stale", PRINTER_UUID])
    controls = control_service(registry, admissions)
    transport = cast(ControlTransport, object())
    controls.register_transport("stale", transport)
    runtime = RegistryRuntimeSupervisor(
        cast(PrinterStore, FakeStore((printer(),))),
        cast(SecretStore, FakeSecrets(secret_values())),
        registry,
        cast(aiohttp.ClientSession, object()),
        admissions=admissions,
        monitor_factory=RecordingFactory(),
        controls=controls,
        control_transport_factory=lambda _record, _key, _session: transport,
    )

    assert await runtime.refresh() == (PRINTER_UUID,)
    assert await registry.get("stale") is None
    assert await registry.get(PRINTER_UUID) is not None
    assert controls.unregister_transport("stale") is False
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_secret_failure_and_collisions_deny_only_affected_printers() -> None:
    first = printer()
    colliding = other_printer(endpoint=first.endpoint)
    healthy = other_printer(
        printer_uuid="33333333-3333-4333-8333-333333333333",
        endpoint=MoonrakerEndpoint(url="http://127.0.0.1:7127", verify_tls=False),
        moonraker_credential_ref="credential-bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb",
        compatibility_credential_ref="credential-cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        safety_profiles=(safety_profile(printer_uuid="33333333-3333-4333-8333-333333333333"),),
    )
    values = secret_values() | {
        "credential-bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb": "z" * 32,
        "credential-cccccccc-cccc-4ccc-8ccc-cccccccccccc": "bad compatibility secret",
    }
    store = FakeStore((first, colliding, healthy))
    secrets = FakeSecrets(values)
    registry = PrinterRegistry([])
    factory = RecordingFactory()
    runtime = supervisor(store, secrets, registry, factory)

    assert await runtime.refresh() == ()
    assert secrets.reads == [
        ("credential-bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb", 32),
        ("credential-cccccccc-cccc-4ccc-8ccc-cccccccccccc", 20),
    ]
    assert factory.calls == []
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_incomplete_active_record_is_denied_before_secret_access() -> None:
    incomplete = other_printer().model_copy(update={"moonraker_credential_ref": None})
    secrets = FakeSecrets(secret_values())
    runtime = supervisor(
        FakeStore((incomplete,)),
        secrets,
        PrinterRegistry([]),
        RecordingFactory(),
    )

    assert await runtime.refresh() == ()
    assert secrets.reads == []
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_unreadable_sibling_and_constructor_failure_do_not_stop_healthy_monitor() -> None:
    first = printer()
    second = other_printer()
    values = secret_values()
    values.pop(OTHER_MOONRAKER_REF)
    store = FakeStore((first, second))
    registry = PrinterRegistry([])
    factory = RecordingFactory()
    secrets = FakeSecrets(values)
    runtime = supervisor(store, secrets, registry, factory)

    assert await runtime.refresh() == (PRINTER_UUID,)
    assert await registry.get(PRINTER_UUID) is not None
    assert await registry.get(OTHER_PRINTER_UUID) is None

    factory.constructor_failure = OTHER_PRINTER_UUID
    secrets.values[OTHER_MOONRAKER_REF] = "n" * 32
    assert await runtime.refresh() == (PRINTER_UUID,)
    assert await registry.get(PRINTER_UUID) is not None
    assert await registry.get(OTHER_PRINTER_UUID) is None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_monitor_failure_removes_only_its_route() -> None:
    first = printer()
    second = other_printer()
    store = FakeStore((first, second))
    registry = PrinterRegistry([])
    admissions = PrinterAdmissionGates()
    controls = control_service(registry, admissions)
    transport = cast(ControlTransport, object())
    factory = RecordingFactory(failures={PRINTER_UUID: RuntimeError()})
    runtime = RegistryRuntimeSupervisor(
        cast(PrinterStore, store),
        cast(SecretStore, FakeSecrets(secret_values())),
        registry,
        cast(aiohttp.ClientSession, object()),
        admissions=admissions,
        monitor_factory=factory,
        controls=controls,
        control_transport_factory=lambda _record, _key, _session: transport,
    )

    assert await runtime.refresh() == (PRINTER_UUID, OTHER_PRINTER_UUID)
    await factory.monitors[0].started.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert await registry.get(PRINTER_UUID) is None
    assert await registry.get(OTHER_PRINTER_UUID) is not None
    assert controls.unregister_transport(PRINTER_UUID) is False
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_stale_control_route_makes_activation_fail_closed_and_is_removed() -> None:
    admissions = PrinterAdmissionGates()
    registry = PrinterRegistry([])
    controls = control_service(registry, admissions)
    transport = cast(ControlTransport, object())
    controls.register_transport(PRINTER_UUID, transport)
    runtime = RegistryRuntimeSupervisor(
        cast(PrinterStore, FakeStore((printer(),))),
        cast(SecretStore, FakeSecrets(secret_values())),
        registry,
        cast(aiohttp.ClientSession, object()),
        admissions=admissions,
        monitor_factory=RecordingFactory(),
        controls=controls,
        control_transport_factory=lambda _record, _key, _session: transport,
    )

    assert await runtime.refresh() == ()
    assert await registry.get(PRINTER_UUID) is None
    assert controls.unregister_transport(PRINTER_UUID) is False
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_route_cleanup_also_operates_when_global_controls_are_disabled() -> None:
    stale_registry = PrinterRegistry(["stale"])
    stale_runtime = supervisor(
        FakeStore(()),
        FakeSecrets({}),
        stale_registry,
        RecordingFactory(),
    )
    assert await stale_runtime.refresh() == ()
    assert await stale_registry.get("stale") is None
    await stale_runtime.shutdown()

    failed_registry = PrinterRegistry([])
    factory = RecordingFactory(failures={PRINTER_UUID: RuntimeError()})
    failed_runtime = supervisor(
        FakeStore((printer(),)),
        FakeSecrets(secret_values()),
        failed_registry,
        factory,
    )
    assert await failed_runtime.refresh() == (PRINTER_UUID,)
    await factory.monitors[0].started.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await failed_registry.get(PRINTER_UUID) is None
    await failed_runtime.shutdown()


@pytest.mark.asyncio
async def test_store_failure_deactivates_every_route_and_is_bounded() -> None:
    store = FakeStore((printer(), other_printer()))
    registry = PrinterRegistry([])
    factory = RecordingFactory()
    runtime = supervisor(store, FakeSecrets(secret_values()), registry, factory)
    assert await runtime.refresh() == (PRINTER_UUID, OTHER_PRINTER_UUID)
    await asyncio.sleep(0)

    store.failure = ValueError("hostile secret text")
    with pytest.raises(RegistryRuntimeError) as caught:
        await runtime.refresh()
    assert str(caught.value) == ""
    assert await registry.list() == ()
    assert all(monitor.stopped.is_set() for monitor in factory.monitors)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_closed_runtime_rejects_refresh() -> None:
    store = FakeStore((printer(),))
    registry = PrinterRegistry([])
    runtime = supervisor(store, FakeSecrets(secret_values()), registry, RecordingFactory())
    await runtime.refresh()
    await asyncio.sleep(0)

    await runtime.shutdown()
    await runtime.shutdown()
    assert await registry.list() == ()
    with pytest.raises(RegistryRuntimeError):
        await runtime.refresh()


@pytest.mark.asyncio
async def test_fresh_supervisor_reloads_the_same_durable_record() -> None:
    store = FakeStore((printer(),))
    secrets = FakeSecrets(secret_values())
    first_registry = PrinterRegistry([])
    first_factory = RecordingFactory()
    first = supervisor(store, secrets, first_registry, first_factory)
    assert await first.refresh() == (PRINTER_UUID,)
    await first.shutdown()

    second_registry = PrinterRegistry([])
    second_factory = RecordingFactory()
    second = supervisor(store, secrets, second_registry, second_factory)
    assert await second.refresh() == (PRINTER_UUID,)
    assert len(first_factory.monitors) == len(second_factory.monitors) == 1
    await second.shutdown()


@pytest.mark.asyncio
async def test_refresh_finishes_safely_when_its_caller_is_cancelled() -> None:
    registry = BlockingRegistry()
    registry.block_register = True
    runtime = supervisor(
        FakeStore((printer(),)),
        FakeSecrets(secret_values()),
        registry,
        RecordingFactory(),
    )
    refresh = asyncio.create_task(runtime.refresh())
    await registry.register_entered.wait()

    refresh.cancel()
    registry.release.set()

    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert await registry.get(PRINTER_UUID) is not None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_finishes_safely_when_its_caller_is_cancelled() -> None:
    registry = BlockingRegistry()
    runtime = supervisor(
        FakeStore((printer(),)),
        FakeSecrets(secret_values()),
        registry,
        RecordingFactory(),
    )
    await runtime.refresh()
    await asyncio.sleep(0)
    registry.block_unregister = True
    shutdown = asyncio.create_task(runtime.shutdown())
    await registry.unregister_entered.wait()

    shutdown.cancel()
    registry.release.set()

    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert await registry.list() == ()
