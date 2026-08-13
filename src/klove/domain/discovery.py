"""Conservative Klipper object capability discovery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable

from klove.domain.models import CapabilitySnapshot

REQUIRED_DISPATCH_OBJECTS = frozenset({"pause_resume", "print_stats", "virtual_sdcard"})
_EXTRUDER_PATTERN = re.compile(r"^extruder\d*$")
_HEATER_PREFIXES = ("heater_generic ",)
_FAN_NAMES = frozenset({"fan"})
_FAN_PREFIXES = ("controller_fan ", "fan_generic ", "heater_fan ")
_FILAMENT_PREFIXES = ("filament_motion_sensor ", "filament_switch_sensor ")


def discover_capabilities(objects: Iterable[str]) -> CapabilitySnapshot:
    """Derive only capabilities proven by exact, documented object names."""
    supplied = tuple(objects)
    if any(not isinstance(name, str) or not name for name in supplied):
        raise ValueError("printer object names must be non-empty strings")
    normalized = frozenset(supplied)
    missing = tuple(sorted(REQUIRED_DISPATCH_OBJECTS - normalized))
    extruders = tuple(sorted(name for name in normalized if _EXTRUDER_PATTERN.fullmatch(name)))
    heaters = tuple(sorted(name for name in normalized if name.startswith(_HEATER_PREFIXES)))
    fans = tuple(
        sorted(name for name in normalized if name in _FAN_NAMES or name.startswith(_FAN_PREFIXES))
    )
    filament_sensors = tuple(
        sorted(name for name in normalized if name.startswith(_FILAMENT_PREFIXES))
    )
    fingerprint_input = json.dumps(sorted(normalized), separators=(",", ":")).encode()
    return CapabilitySnapshot(
        objects=normalized,
        missing_dispatch_objects=missing,
        extruders=extruders,
        heaters=heaters,
        fans=fans,
        filament_sensors=filament_sensors,
        has_heated_bed="heater_bed" in normalized,
        has_exclude_object="exclude_object" in normalized,
        dispatch_eligible=not missing,
        fingerprint=hashlib.sha256(fingerprint_input).hexdigest(),
    )
