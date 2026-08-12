import pytest

from klove.domain.discovery import discover_capabilities


def test_capability_discovery_uses_exact_known_names() -> None:
    result = discover_capabilities(
        [
            "pause_resume",
            "print_stats",
            "virtual_sdcard",
            "extruder",
            "extruder1",
            "heater_bed",
            "heater_generic chamber",
            "fan",
            "fan_generic aux",
            "heater_fan hotend",
            "controller_fan electronics",
            "filament_switch_sensor runout",
            "filament_motion_sensor encoder",
            "exclude_object",
            "my_fan_impostor",
        ]
    )

    assert result.dispatch_eligible
    assert result.missing_dispatch_objects == ()
    assert result.extruders == ("extruder", "extruder1")
    assert result.heaters == ("heater_generic chamber",)
    assert result.fans == (
        "controller_fan electronics",
        "fan",
        "fan_generic aux",
        "heater_fan hotend",
    )
    assert result.filament_sensors == (
        "filament_motion_sensor encoder",
        "filament_switch_sensor runout",
    )
    assert result.has_heated_bed
    assert result.has_exclude_object
    assert len(result.fingerprint) == 64


def test_missing_required_object_denies_dispatch_and_changes_fingerprint() -> None:
    complete = discover_capabilities(["pause_resume", "print_stats", "virtual_sdcard"])
    incomplete = discover_capabilities(["print_stats"])

    assert incomplete.missing_dispatch_objects == ("pause_resume", "virtual_sdcard")
    assert not incomplete.dispatch_eligible
    assert complete.fingerprint != incomplete.fingerprint


@pytest.mark.parametrize("objects", [[""], ["print_stats", 1]])
def test_invalid_object_names_are_rejected(objects: list[object]) -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        discover_capabilities(objects)  # type: ignore[arg-type]
