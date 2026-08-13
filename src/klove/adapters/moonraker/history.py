"""Strict decoding of Moonraker's immutable job-history identities."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from klove.domain.models import JobHistoryStatus, JobIdentitySnapshot
from klove.errors import ProtocolError


def decode_history_list(value: object) -> JobIdentitySnapshot | None:
    """Decode the one newest history entry requested with ``limit=1``."""
    if not isinstance(value, dict) or set(value) != {"count", "jobs"}:
        raise ProtocolError("history list returned an unexpected shape")
    count = value["count"]
    jobs = value["jobs"]
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not isinstance(jobs, list)
        or count != len(jobs)
        or len(jobs) > 1
    ):
        raise ProtocolError("history list returned an invalid result")
    return None if not jobs else _decode_job(jobs[0])


def decode_history_notification(params: object) -> JobIdentitySnapshot:
    """Decode one job-added or job-finished notification."""
    if not isinstance(params, list) or len(params) != 1 or not isinstance(params[0], dict):
        raise ProtocolError("history notification is malformed")
    event = params[0]
    if set(event) != {"action", "job"} or event["action"] not in {"added", "finished"}:
        raise ProtocolError("history notification is malformed")
    job = _decode_job(event["job"])
    if (event["action"] == "added") is not (job.status is JobHistoryStatus.IN_PROGRESS):
        raise ProtocolError("history notification action contradicts job status")
    return job


def _decode_job(value: Any) -> JobIdentitySnapshot:
    if not isinstance(value, dict):
        raise ProtocolError("history job is not an object")
    job_id = value.get("job_id")
    filename = value.get("filename")
    start_time = value.get("start_time")
    status = value.get("status")
    if (
        not isinstance(job_id, str)
        or not isinstance(filename, str)
        or isinstance(start_time, bool)
        or not isinstance(start_time, (int, float))
        or not isinstance(status, str)
    ):
        raise ProtocolError("history job identity has invalid field types")
    try:
        return JobIdentitySnapshot.model_validate(
            {
                "job_id": job_id,
                "filename": filename,
                "start_time": start_time,
                "status": status,
            }
        )
    except ValidationError as exc:
        raise ProtocolError("history job identity is invalid") from exc
