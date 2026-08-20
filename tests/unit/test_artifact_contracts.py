"""Facade compatibility: `klove.domain.artifacts` re-exports the split internal modules.

Model- and schema-specific coverage lives in `test_artifact_contract_models.py`;
qualification-policy coverage lives in `test_artifact_policy.py`.
"""

from __future__ import annotations

from pathlib import Path

import klove.domain.artifact_contract_models as contract_models
import klove.domain.artifact_policy as policy
import klove.domain.artifacts as facade

FIXTURES = Path(__file__).parents[1] / "fixtures" / "artifacts"


def test_facade_reexports_are_the_exact_same_contract_model_objects() -> None:
    for name in facade.__all__:
        if hasattr(contract_models, name):
            assert getattr(facade, name) is getattr(contract_models, name)
        else:
            assert hasattr(policy, name)
            assert getattr(facade, name) is getattr(policy, name)


def test_facade_end_to_end_intake_through_qualification() -> None:
    intent = facade.ArtifactIntent.model_validate_json(
        (FIXTURES / "accepted-intent.json").read_text(encoding="utf-8")
    )
    inspection = facade.ArtifactInspectionEvidence.model_validate_json(
        (FIXTURES / "accepted-evidence.json").read_text(encoding="utf-8")
    )
    approval = facade.ArtifactTargetApproval.model_validate_json(
        (FIXTURES / "accepted-approval.json").read_text(encoding="utf-8")
    )
    profile = facade.SafetyProfile(
        printer_uuid="11111111-1111-4111-8111-111111111111",
        generation=7,
        slicer_profile_id="klipper-voron-24-0.4",
        compatibility=facade.ArtifactCompatibility(
            gcode_flavor=facade.GcodeFlavor.KLIPPER,
            nozzle_diameter_micrometres=400,
            build_volume=facade.BuildVolume(
                x_micrometres=350_000,
                y_micrometres=350_000,
                z_micrometres=350_000,
            ),
            build_plate_id="textured-pei",
        ),
    )

    result = facade.assess_artifact_intent(
        intent,
        (inspection,),
        (approval,),
        current_profile=profile,
        manual_override=None,
        limits=facade.ArtifactLimits(),
    )

    assert result.state is facade.ArtifactOperationState.QUALIFIED
    assert result.qualification is not None
    assert result.qualification.target == facade.target_for_safety_profile(profile)
