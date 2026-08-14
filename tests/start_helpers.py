from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from klove.config import PrinterConfig
from klove.domain.artifacts import (
    ArtifactCompatibility,
    ArtifactQualification,
    ArtifactTargetApproval,
    BuildVolume,
    GcodeFlavor,
    SafetyProfile,
)
from klove.domain.models import JobHistoryStatus, JobIdentitySnapshot, PrinterPhase
from klove.domain.start import StartObservation, StartPreflight
from klove.domain.upload import (
    MoonrakerGcodeMetadata,
    MoonrakerUploadReceipt,
    RemoteFileDigest,
    VerifiedUpload,
)

FIXTURES = Path(__file__).parent / "fixtures" / "artifacts"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
IDEMPOTENCY_KEY = "33333333-3333-4333-8333-333333333333"
PRINTER_UUID = "11111111-1111-4111-8111-111111111111"
PATH = f"klove/{OPERATION_ID}.gcode"
MODIFIED = Decimal("1700000000.25")
METADATA_UUID = "44444444-4444-4444-8444-444444444444"


def safety_profile(**updates: object) -> SafetyProfile:
    values: dict[str, object] = {
        "printer_uuid": PRINTER_UUID,
        "generation": 7,
        "slicer_profile_id": "klipper-voron-24-0.4",
        "compatibility": ArtifactCompatibility(
            gcode_flavor=GcodeFlavor.KLIPPER,
            nozzle_diameter_micrometres=400,
            build_volume=BuildVolume(
                x_micrometres=350_000,
                y_micrometres=350_000,
                z_micrometres=350_000,
            ),
            build_plate_id="textured-pei",
        ),
    }
    values.update(updates)
    return SafetyProfile.model_validate(values)


def printer_config(*, profiles: tuple[SafetyProfile, ...] | None = None) -> PrinterConfig:
    return PrinterConfig(
        id="voron",
        uuid=PRINTER_UUID,
        endpoint="http://127.0.0.1:7125",
        api_key_file=Path("unused"),
        allow_insecure_http=True,
        dispatch_enabled=True,
        safety_profiles=(safety_profile(),) if profiles is None else profiles,
    )


def qualification(
    *,
    operation_id: str = OPERATION_ID,
    idempotency_key: str = IDEMPOTENCY_KEY,
) -> ArtifactQualification:
    approval = ArtifactTargetApproval.model_validate_json(
        (FIXTURES / "accepted-approval.json").read_text(encoding="utf-8")
    )
    return ArtifactQualification(
        approval_id=approval.approval_id,
        authority_id=approval.authority_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        artifact=approval.artifact,
        selected_plate=approval.selected_plate,
        target=approval.target,
    )


def verified_upload(
    *,
    operation_id: str = OPERATION_ID,
    idempotency_key: str = IDEMPOTENCY_KEY,
) -> VerifiedUpload:
    qualified = qualification(operation_id=operation_id, idempotency_key=idempotency_key)
    path = f"klove/{operation_id}.gcode"
    size = qualified.selected_plate.gcode_size_bytes
    return VerifiedUpload(
        path=path,
        qualification=qualified,
        receipt=MoonrakerUploadReceipt(path=path, size_bytes=size, modified=MODIFIED),
        metadata=MoonrakerGcodeMetadata(
            path=path,
            size_bytes=size,
            modified=MODIFIED,
            metadata_uuid=METADATA_UUID,
            file_processors=(),
            nozzle_diameter_micrometres=400,
            gcode_start_byte=10,
            gcode_end_byte=size,
            job_id=None,
            print_start_time=None,
        ),
        remote_file=RemoteFileDigest(
            size_bytes=size,
            sha256=qualified.selected_plate.gcode_sha256,
        ),
    )


def history_job(
    *,
    job_id: str = "000002",
    filename: str = PATH,
    start_time: float = 1_700_000_100.0,
    status: JobHistoryStatus = JobHistoryStatus.IN_PROGRESS,
) -> JobIdentitySnapshot:
    return JobIdentitySnapshot(
        job_id=job_id,
        filename=filename,
        start_time=start_time,
        status=status,
    )


def observation(
    *,
    eventtime: float = 11.0,
    phase: PrinterPhase = PrinterPhase.PRINTING,
    filename: str = PATH,
    file_position: int = 100,
    latest_job: JobIdentitySnapshot | None = None,
) -> StartObservation:
    return StartObservation(
        eventtime=eventtime,
        phase=phase,
        filename=filename,
        file_position=file_position,
        latest_job=(
            history_job() if latest_job is None and phase is not PrinterPhase.IDLE else latest_job
        ),
    )


def preflight(*, prior_job: JobIdentitySnapshot | None = None) -> StartPreflight:
    return StartPreflight(
        observation=observation(
            eventtime=10.0,
            phase=PrinterPhase.IDLE,
            filename="previous.gcode" if prior_job is not None else "",
            file_position=0,
            latest_job=prior_job,
        )
    )
