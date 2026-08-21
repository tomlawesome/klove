"""Immutable history append plans for canonical registry transitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from klove.domain.artifacts import SafetyProfile, safety_profile_fingerprint
from klove.domain.onboarding import PrinterIdentityEvidence, RegisteredPrinter


class RegistryHistoryValidationError(ValueError):
    """A transition cannot produce a valid immutable-history append."""


class ProfileHistoryEvent(StrEnum):
    """The closed set of immutable profile-history events."""

    BASELINE = "baseline"
    BOUND = "bound"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class MappingHistoryAppend:
    """One complete direct identity/capability observation to append."""

    printer_uuid: str
    registry_revision: int
    observed_at_unix_ms: int
    mapping_fingerprint: str
    snapshot_json: str


@dataclass(frozen=True, slots=True)
class ProfileHistoryAppend:
    """One exact safety-profile event to append."""

    printer_uuid: str
    registry_revision: int
    slicer_profile_id: str
    generation: int
    profile_fingerprint: str
    event: ProfileHistoryEvent
    snapshot_json: str


@dataclass(frozen=True, slots=True)
class RegistryHistoryAppendPlan:
    """All history rows belonging to one canonical registry revision."""

    mapping: MappingHistoryAppend | None
    profiles: tuple[ProfileHistoryAppend, ...]


def build_registry_history_append_plan(
    current: RegisteredPrinter | None,
    result: RegisteredPrinter,
    retained_profile_generations: Mapping[str, int] | None = None,
) -> RegistryHistoryAppendPlan:
    """Build the exact immutable-history rows for one accepted transition.

    ``retained_profile_generations`` is supplied by the schema adapter from
    all retained history rows.  The adapter must query it while the caller's
    optimistic transaction is open.  It is intentionally not read from the
    current JSON record because retired generations are not current authority.
    """
    if current is not None and current.printer_uuid != result.printer_uuid:
        raise RegistryHistoryValidationError("history transition printer UUID mismatch")
    retained = _validated_retained_generations(retained_profile_generations)
    mapping = _mapping_append(current, result)
    profiles = _profile_appends(current, result, retained)
    return RegistryHistoryAppendPlan(mapping=mapping, profiles=profiles)


def mapping_fingerprint(identity: PrinterIdentityEvidence) -> str:
    """Hash identity/capability evidence while excluding observation time."""
    material = identity.model_dump(mode="python", exclude={"observed_at_unix_ms"})
    canonical = _canonical_json(material)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def canonical_identity_json(identity: PrinterIdentityEvidence) -> str:
    """Encode one complete identity snapshot in stable canonical JSON."""
    return _canonical_json(identity.model_dump(mode="python"))


def canonical_profile_json(profile: SafetyProfile) -> str:
    """Encode one complete safety profile in stable canonical JSON."""
    return _canonical_json(profile.model_dump(mode="python"))


def _mapping_append(
    current: RegisteredPrinter | None, result: RegisteredPrinter
) -> MappingHistoryAppend | None:
    if current is not None:
        current_fingerprint = mapping_fingerprint(current.identity)
        result_fingerprint = mapping_fingerprint(result.identity)
        if result.identity.observed_at_unix_ms < current.identity.observed_at_unix_ms:
            raise RegistryHistoryValidationError("identity observation moved backwards")
        if current_fingerprint == result_fingerprint:
            return None
        if result.identity.observed_at_unix_ms <= current.identity.observed_at_unix_ms:
            raise RegistryHistoryValidationError(
                "material identity change requires a later observation"
            )
    else:
        result_fingerprint = mapping_fingerprint(result.identity)
    return MappingHistoryAppend(
        printer_uuid=result.printer_uuid,
        registry_revision=result.revision,
        observed_at_unix_ms=result.identity.observed_at_unix_ms,
        mapping_fingerprint=result_fingerprint,
        snapshot_json=canonical_identity_json(result.identity),
    )


def _profile_appends(
    current: RegisteredPrinter | None,
    result: RegisteredPrinter,
    retained: Mapping[str, int],
) -> tuple[ProfileHistoryAppend, ...]:
    previous = {} if current is None else _profiles_by_id(current.safety_profiles)
    next_profiles = _profiles_by_id(result.safety_profiles)
    events: list[ProfileHistoryAppend] = []
    for profile_id in sorted(set(previous) | set(next_profiles)):
        old = previous.get(profile_id)
        new = next_profiles.get(profile_id)
        if old == new:
            continue
        if old is not None and new is not None:
            _require_new_generation(new, old, retained)
            events.append(_profile_append(result, old, ProfileHistoryEvent.RETIRED))
            events.append(_profile_append(result, new, ProfileHistoryEvent.BOUND))
        elif new is not None:
            _require_not_reused_below_retained(new, retained)
            events.append(_profile_append(result, new, ProfileHistoryEvent.BOUND))
        else:
            events.append(_profile_append(result, old, ProfileHistoryEvent.RETIRED))
    return tuple(events)


def _profiles_by_id(profiles: tuple[SafetyProfile, ...]) -> dict[str, SafetyProfile]:
    return {profile.slicer_profile_id: profile for profile in profiles}


def _require_new_generation(
    new: SafetyProfile,
    old: SafetyProfile,
    retained: Mapping[str, int],
) -> None:
    retained_max = max(old.generation, retained.get(new.slicer_profile_id, old.generation))
    if new.generation <= retained_max:
        raise RegistryHistoryValidationError(
            "profile rebinding requires a generation above retained history"
        )


def _require_not_reused_below_retained(profile: SafetyProfile, retained: Mapping[str, int]) -> None:
    retained_max = retained.get(profile.slicer_profile_id)
    if retained_max is not None and profile.generation <= retained_max:
        raise RegistryHistoryValidationError(
            "profile rebinding requires a generation above retained history"
        )


def _profile_append(
    printer: RegisteredPrinter,
    profile: SafetyProfile | None,
    event: ProfileHistoryEvent,
) -> ProfileHistoryAppend:
    if profile is None:
        raise RegistryHistoryValidationError("profile retirement has no former profile")
    return ProfileHistoryAppend(
        printer_uuid=printer.printer_uuid,
        registry_revision=printer.revision,
        slicer_profile_id=profile.slicer_profile_id,
        generation=profile.generation,
        profile_fingerprint=safety_profile_fingerprint(profile),
        event=event,
        snapshot_json=canonical_profile_json(profile),
    )


def _validated_retained_generations(
    generations: Mapping[str, int] | None,
) -> dict[str, int]:
    if generations is None:
        return {}
    if not isinstance(generations, Mapping):
        raise RegistryHistoryValidationError("retained generations are not a mapping")
    result: dict[str, int] = {}
    for profile_id, generation in generations.items():
        if (
            type(profile_id) is not str
            or not profile_id
            or type(generation) is not int
            or generation < 1
        ):
            raise RegistryHistoryValidationError("retained profile generation is invalid")
        result[profile_id] = generation
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value
