from __future__ import annotations

import pytest

from klove.adapters.moonraker.history import (
    decode_history_list,
    decode_history_notification,
)
from klove.domain.models import JobHistoryStatus, JobIdentitySnapshot
from klove.errors import ProtocolError


def job(status: str = "in_progress") -> dict[str, object]:
    return {
        "job_id": "000001",
        "filename": "job.gcode",
        "start_time": 1_700_000_000.0,
        "status": status,
        "ignored_history_field": True,
    }


def test_history_list_decodes_zero_or_one_exact_identity() -> None:
    assert decode_history_list({"count": 0, "jobs": []}) is None
    assert decode_history_list({"count": 1, "jobs": [job()]}) == JobIdentitySnapshot(
        job_id="000001",
        filename="job.gcode",
        start_time=1_700_000_000.0,
        status=JobHistoryStatus.IN_PROGRESS,
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"count": 0, "jobs": [], "extra": True},
        {"count": True, "jobs": []},
        {"count": 0.0, "jobs": []},
        {"count": 0, "jobs": {}},
        {"count": 0, "jobs": [job()]},
        {"count": 2, "jobs": [job(), job()]},
        {"count": 1, "jobs": [None]},
        {"count": 1, "jobs": [{**job(), "job_id": 1}]},
        {"count": 1, "jobs": [{**job(), "job_id": "1"}]},
        {"count": 1, "jobs": [{**job(), "filename": 1}]},
        {"count": 1, "jobs": [{**job(), "filename": ""}]},
        {"count": 1, "jobs": [{**job(), "start_time": True}]},
        {"count": 1, "jobs": [{**job(), "start_time": "1700000000"}]},
        {"count": 1, "jobs": [{**job(), "start_time": -1}]},
        {"count": 1, "jobs": [{**job(), "start_time": float("inf")}]},
        {"count": 1, "jobs": [{**job(), "status": 1}]},
        {"count": 1, "jobs": [{**job(), "status": "unknown"}]},
    ],
)
def test_history_list_rejects_every_ambiguous_identity(value: object) -> None:
    with pytest.raises(ProtocolError):
        decode_history_list(value)


def test_history_notifications_decode_added_and_finished_jobs() -> None:
    added = decode_history_notification([{"action": "added", "job": job()}])
    finished = decode_history_notification([{"action": "finished", "job": job("completed")}])

    assert added.status is JobHistoryStatus.IN_PROGRESS
    assert finished.status is JobHistoryStatus.COMPLETED


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        [{}, {}],
        [None],
        [{"action": "added"}],
        [{"action": "unknown", "job": job()}],
        [{"action": "added", "job": job("completed")}],
        [{"action": "finished", "job": job()}],
    ],
)
def test_history_notifications_reject_malformed_or_contradictory_events(
    params: object,
) -> None:
    with pytest.raises(ProtocolError):
        decode_history_notification(params)
