"""Read-only restart proof for the native ADR-0007 dispatch fixture."""

from __future__ import annotations

import asyncio

from dispatch_contract import (
    AMBIGUOUS_IDEMPOTENCY_KEY,
    AMBIGUOUS_OPERATION_ID,
    GCODE,
    GRANT,
    _archive,
    _count,
    _read_api_key,
    _request,
    _require,
    _runtime,
    _wait_for_completed,
    _wait_for_idle,
    _wait_state,
)
from sdcard_reset import reset_virtual_sd_after_completed_print

from klove.domain.dispatch import DispatchState


async def _main() -> None:
    """Reconcile the persisted uncertain start without submitting or dispatching."""
    archive = _archive()
    ambiguous = _request(AMBIGUOUS_OPERATION_ID, AMBIGUOUS_IDEMPOTENCY_KEY, archive)
    runtime = await _runtime()
    try:
        completed = await _wait_state(runtime.coordinator, ambiguous, DispatchState.COMPLETED)
        _require(
            completed.remote_path == f"klove/{ambiguous.operation_id}.gcode",
            "restart reconciliation lost the exact remote path",
        )
        _require(
            _count("server.files.upload") == 2,
            "restart reconciliation performed an upload",
        )
        _require(
            _count("printer.print.start") == 2,
            "restart reconciliation retried a potentially-dispatched start",
        )
        completed_job = await _wait_for_completed(
            runtime.transport,
            expected_path=f"klove/{ambiguous.operation_id}.gcode",
            expected_size=len(GCODE),
        )
        await reset_virtual_sd_after_completed_print(runtime.session, _read_api_key())
        await _wait_for_idle(
            runtime.transport,
            expected_latest_job=completed_job,
        )
        _require(
            await runtime.coordinator.result(
                GRANT, ambiguous.operation_id, ambiguous.idempotency_key
            )
            == completed,
            "read-only result lookup changed recovered dispatch evidence",
        )
        print("native dispatch reconnect contract passed", flush=True)
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(_main())
