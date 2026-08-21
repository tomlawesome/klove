from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from klove.domain.onboarding import (
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.domain.registry_history import (
    ProfileHistoryEvent,
    RegistryHistoryAppendPlan,
    RegistryHistoryValidationError,
    _profile_append,
    build_registry_history_append_plan,
    mapping_fingerprint,
)
from klove.persistence.printer_registry_errors import RegistryTransitionError
from klove.persistence.printer_registry_transactions import _commit_transaction

from ..onboarding_helpers import (
    OTHER_PRINTER_UUID,
    identity,
    make_stores,
    operation,
    printer,
    safety_profile,
    write_printer_secrets,
)


def _committed(
    pending: RegistryOperationRecord, result: RegisteredPrinter
) -> RegistryOperationRecord:
    return RegistryOperationRecord.model_validate(
        {
            **pending.model_dump(mode="python"),
            "state": RegistryOperationState.COMMITTED,
            "result": result,
            "committed_at_unix_ms": result.updated_at_unix_ms,
            "error_code": None,
        }
    )


def test_mapping_timestamp_is_not_material_history() -> None:
    current = printer()
    fresher = current.model_copy(
        update={
            "identity": identity().model_copy(update={"observed_at_unix_ms": 901}),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    plan = build_registry_history_append_plan(current, fresher)

    assert mapping_fingerprint(current.identity) == mapping_fingerprint(fresher.identity)
    assert plan.mapping is None
    assert plan.profiles == ()


def test_history_rejects_mismatched_printer_and_invalid_retained_generations() -> None:
    current = printer()
    other = printer(
        printer_uuid=OTHER_PRINTER_UUID,
        safety_profiles=(),
        control_enabled=False,
        dispatch_enabled=False,
    )

    with pytest.raises(RegistryHistoryValidationError):
        build_registry_history_append_plan(current, other)
    with pytest.raises(RegistryHistoryValidationError):
        build_registry_history_append_plan(current, current, cast(Mapping[str, int], ["not-a-map"]))
    with pytest.raises(RegistryHistoryValidationError):
        build_registry_history_append_plan(current, current, {"profile": 0})


def test_profile_event_requires_a_former_profile() -> None:
    with pytest.raises(RegistryHistoryValidationError):
        _profile_append(printer(), None, ProfileHistoryEvent.RETIRED)


@pytest.mark.parametrize("observed_at", [899, 900])
def test_material_mapping_change_requires_strictly_later_observation(
    observed_at: int,
) -> None:
    current = printer()
    changed = current.model_copy(
        update={
            "identity": identity().model_copy(
                update={"server_hostname": "192.0.2.10", "observed_at_unix_ms": observed_at}
            ),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    with pytest.raises(RegistryHistoryValidationError):
        build_registry_history_append_plan(current, changed)


def test_material_mapping_change_appends_one_complete_snapshot() -> None:
    current = printer()
    changed = current.model_copy(
        update={
            "identity": identity().model_copy(
                update={"server_hostname": "192.0.2.10", "observed_at_unix_ms": 901}
            ),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    mapping = build_registry_history_append_plan(current, changed).mapping

    assert mapping is not None
    assert mapping.registry_revision == 2
    assert mapping.observed_at_unix_ms == 901
    assert mapping.mapping_fingerprint == mapping_fingerprint(changed.identity)
    assert '"observed_at_unix_ms":901' in mapping.snapshot_json


def test_create_binds_profiles_and_mapping() -> None:
    result = printer()

    plan = build_registry_history_append_plan(None, result)

    assert plan.mapping is not None
    assert [(event.event, event.generation) for event in plan.profiles] == [
        (ProfileHistoryEvent.BOUND, 1)
    ]


def test_profile_add_and_remove_are_bound_and_retired() -> None:
    current = printer(safety_profiles=(), control_enabled=False, dispatch_enabled=False)
    added = current.model_copy(
        update={
            "safety_profiles": (safety_profile(),),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    added_plan = build_registry_history_append_plan(current, added)
    removed_plan = build_registry_history_append_plan(added, current)

    assert [(event.event, event.slicer_profile_id) for event in added_plan.profiles] == [
        (ProfileHistoryEvent.BOUND, "klipper-voron-24-0.4")
    ]
    assert [(event.event, event.slicer_profile_id) for event in removed_plan.profiles] == [
        (ProfileHistoryEvent.RETIRED, "klipper-voron-24-0.4")
    ]


def test_exact_profile_update_does_not_append() -> None:
    current = printer()
    unchanged = current.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_400})

    assert build_registry_history_append_plan(current, unchanged).profiles == ()


def test_profile_rebinding_retire_then_bind_requires_new_generation() -> None:
    current = printer()
    replacement = safety_profile().model_copy(
        update={
            "generation": 2,
            "compatibility": safety_profile().compatibility.model_copy(
                update={"build_plate_id": "glass"}
            ),
        }
    )
    changed = current.model_copy(
        update={
            "safety_profiles": (replacement,),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    events = build_registry_history_append_plan(
        current, changed, {replacement.slicer_profile_id: 1}
    ).profiles

    assert [(event.event, event.generation) for event in events] == [
        (ProfileHistoryEvent.RETIRED, 1),
        (ProfileHistoryEvent.BOUND, 2),
    ]


def test_profile_rebinding_cannot_reuse_retained_generation() -> None:
    current = printer()
    replacement = safety_profile().model_copy(
        update={
            "generation": 2,
            "compatibility": safety_profile().compatibility.model_copy(
                update={"build_plate_id": "glass"}
            ),
        }
    )
    changed = current.model_copy(
        update={
            "safety_profiles": (replacement,),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    with pytest.raises(RegistryHistoryValidationError):
        build_registry_history_append_plan(current, changed, {replacement.slicer_profile_id: 7})


def test_rebinding_a_retired_id_uses_retained_maximum() -> None:
    current = printer(safety_profiles=(), control_enabled=False, dispatch_enabled=False)
    rebound = safety_profile().model_copy(update={"generation": 4})
    result = current.model_copy(
        update={
            "safety_profiles": (rebound,),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    events = build_registry_history_append_plan(
        current, result, {rebound.slicer_profile_id: 3}
    ).profiles

    assert len(events) == 1
    assert events[0].event is ProfileHistoryEvent.BOUND
    assert events[0].generation == 4


def test_rebinding_a_retired_id_cannot_reuse_retained_generation() -> None:
    current = printer(safety_profiles=(), control_enabled=False, dispatch_enabled=False)
    rebound = safety_profile().model_copy(update={"generation": 3})
    result = current.model_copy(
        update={
            "safety_profiles": (rebound,),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    with pytest.raises(RegistryHistoryValidationError):
        build_registry_history_append_plan(current, result, {rebound.slicer_profile_id: 3})


class _RecordingHistoryWriter:
    def __init__(self, retained: dict[str, int] | None = None) -> None:
        self.retained = {} if retained is None else retained
        self.plans: list[RegistryHistoryAppendPlan] = []

    def retained_profile_generations(
        self,
        connection: sqlite3.Connection,
        printer_uuid: str,
        profile_ids: tuple[str, ...],
    ) -> dict[str, int]:
        del connection, printer_uuid, profile_ids
        return self.retained

    def append(self, connection: sqlite3.Connection, plan: RegistryHistoryAppendPlan) -> None:
        self.plans.append(plan)
        connection.execute("INSERT INTO history_sink DEFAULT VALUES")


def test_history_writer_runs_inside_atomic_registry_transaction(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    pending_create = operation()
    store.reserve(pending_create)
    write_printer_secrets(secrets)
    current = store.commit(pending_create, printer(), committed_at_unix_ms=1_000).result
    assert current is not None

    pending = operation(
        idempotency_key="44444444-4444-4444-8444-444444444444",
        operation=RegistryOperationKind.UPDATE,
        expected_revision=current.revision,
        new_credential_refs=(),
        started_at_unix_ms=1_100,
    )
    store.reserve(pending)
    result = current.model_copy(
        update={
            "identity": identity().model_copy(
                update={"server_hostname": "192.0.2.10", "observed_at_unix_ms": 1_101}
            ),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )
    writer = _RecordingHistoryWriter()
    with closing(store._connect()) as connection:
        connection.execute("CREATE TABLE history_sink (id INTEGER)")
        _commit_transaction(
            connection, pending, result, _committed(pending, result), history=writer
        )

    assert len(writer.plans) == 1
    assert writer.plans[0].mapping is not None
    assert store.get(result.printer_uuid) == result


def test_history_validation_rolls_back_current_and_operation(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    pending_create = operation()
    store.reserve(pending_create)
    write_printer_secrets(secrets)
    current = store.commit(pending_create, printer(), committed_at_unix_ms=1_000).result
    assert current is not None
    pending = operation(
        idempotency_key="44444444-4444-4444-8444-444444444444",
        operation=RegistryOperationKind.UPDATE,
        expected_revision=current.revision,
        new_credential_refs=(),
        started_at_unix_ms=1_100,
    )
    store.reserve(pending)
    replacement = safety_profile().model_copy(
        update={
            "generation": 2,
            "compatibility": safety_profile().compatibility.model_copy(
                update={"build_plate_id": "glass"}
            ),
        }
    )
    result = current.model_copy(
        update={
            "safety_profiles": (replacement,),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    with closing(store._connect()) as connection, pytest.raises(RegistryTransitionError):
        _commit_transaction(
            connection,
            pending,
            result,
            _committed(pending, result),
            history=_RecordingHistoryWriter({replacement.slicer_profile_id: 7}),
        )

    assert store.get(result.printer_uuid) == current
    assert store.lookup_operation(pending.idempotency_key) == pending
