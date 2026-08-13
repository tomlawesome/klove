from __future__ import annotations

from klove.domain.models import PrinterPhase, initial_snapshot


def test_state_token_is_stable_opaque_and_bound_to_boot_revision_and_job() -> None:
    baseline = initial_snapshot("voron")
    same = initial_snapshot("voron")
    advanced = baseline.model_copy(
        update={
            "revision": 1,
            "phase": PrinterPhase.PRINTING,
            "eventtime": 10.0,
            "status": {
                "print_stats": {"filename": "job.gcode"},
                "virtual_sdcard": {"file_position": 100},
            },
        }
    )
    moved = advanced.model_copy(
        update={"status": {**advanced.status, "virtual_sdcard": {"file_position": 101}}}
    )
    another_boot = advanced.model_copy(update={"epoch": "00000000-0000-4000-8000-000000000000"})

    assert baseline.epoch == same.epoch
    assert baseline.state_token == same.state_token
    assert len(baseline.state_token) == 64
    assert baseline.epoch not in baseline.state_token
    assert (
        len(
            {
                baseline.state_token,
                advanced.state_token,
                moved.state_token,
                another_boot.state_token,
            }
        )
        == 4
    )
