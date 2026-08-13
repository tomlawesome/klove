from __future__ import annotations

import pytest
from pydantic import ValidationError

from klove.domain.discovery import discover_capabilities
from klove.domain.models import (
    JobHistoryStatus,
    JobIdentitySnapshot,
    PrinterPhase,
    initial_snapshot,
)


def test_state_token_is_opaque_and_bound_to_exact_control_state() -> None:
    baseline = initial_snapshot("voron")
    same = initial_snapshot("voron")
    printing = baseline.model_copy(
        update={
            "revision": 1,
            "control_revision": 1,
            "connected": True,
            "phase": PrinterPhase.PRINTING,
            "reason": "observed",
            "eventtime": 10.0,
            "capabilities": discover_capabilities(
                {"pause_resume", "print_stats", "virtual_sdcard"}
            ),
            "job": JobIdentitySnapshot(
                job_id="000001",
                filename="job.gcode",
                start_time=1_700_000_000.0,
                status=JobHistoryStatus.IN_PROGRESS,
            ),
            "status": {
                "print_stats": {"filename": "job.gcode"},
                "virtual_sdcard": {"file_position": 100},
            },
        }
    )
    telemetry_advanced = printing.model_copy(
        update={
            "revision": 2,
            "eventtime": 11.0,
            "status": {
                **printing.status,
                "virtual_sdcard": {"file_position": 101},
            },
        }
    )
    next_control_state = telemetry_advanced.model_copy(update={"control_revision": 2})
    another_job_instance = printing.model_copy(
        update={
            "job": printing.job.model_copy(update={"start_time": 1_700_000_001.0})
            if printing.job is not None
            else None
        }
    )
    another_job = printing.model_copy(
        update={"status": {**printing.status, "print_stats": {"filename": "other.gcode"}}}
    )
    another_capability = printing.model_copy(
        update={
            "capabilities": discover_capabilities(
                {"exclude_object", "pause_resume", "print_stats", "virtual_sdcard"}
            )
        }
    )
    another_boot = printing.model_copy(update={"epoch": "00000000-0000-4000-8000-000000000000"})

    assert baseline.epoch == same.epoch
    assert baseline.state_token == same.state_token
    assert printing.state_token == telemetry_advanced.state_token
    assert len(baseline.state_token) == 64
    assert baseline.epoch not in baseline.state_token
    assert (
        len(
            {
                baseline.state_token,
                printing.state_token,
                next_control_state.state_token,
                another_job_instance.state_token,
                another_job.state_token,
                another_capability.state_token,
                another_boot.state_token,
            }
        )
        == 7
    )


def test_job_identity_rejects_boolean_start_time() -> None:
    with pytest.raises(ValidationError):
        JobIdentitySnapshot(
            job_id="000001",
            filename="job.gcode",
            start_time=True,
            status=JobHistoryStatus.IN_PROGRESS,
        )
