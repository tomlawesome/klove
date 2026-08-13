import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from klove.domain.translation import (
    CommandDenial,
    CommandKind,
    DecodedCommand,
    decode_grove_request,
    deny_actuation,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "grove"


@pytest.mark.parametrize(
    ("fixture", "kind", "sequence_id"),
    [
        ("pause.json", CommandKind.PAUSE, "0"),
        ("resume.json", CommandKind.RESUME, "17"),
        ("stop.json", CommandKind.CANCEL, "18"),
    ],
)
def test_known_grove_job_control_commands_are_typed(
    fixture: str, kind: CommandKind, sequence_id: str
) -> None:
    payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    result = decode_grove_request(payload)

    assert isinstance(result, DecodedCommand)
    assert result.kind is kind
    assert result.sequence_id == sequence_id
    assert len(result.payload_hash) == 64
    assert deny_actuation(result) == CommandDenial(code="actuation_disabled")


def test_arbitrary_gcode_is_never_decoded() -> None:
    payload = json.loads((FIXTURES / "arbitrary-gcode.json").read_text(encoding="utf-8"))
    assert decode_grove_request(payload) == CommandDenial(code="unexpected_fields")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (None, "invalid_envelope"),
        ({}, "invalid_envelope"),
        ({"print": {}, "system": {}}, "invalid_envelope"),
        ({"print": "pause"}, "invalid_print_block"),
        ({"print": {"command": "pause"}}, "unexpected_fields"),
        (
            {"print": {"command": "pause", "sequence_id": "0", "extra": True}},
            "unexpected_fields",
        ),
        ({"print": {"command": 1, "sequence_id": "0"}}, "unsupported_command"),
        ({"print": {"command": "project_file", "sequence_id": "0"}}, "unsupported_command"),
        ({"print": {"command": "pause", "sequence_id": 0}}, "invalid_sequence_id"),
        ({"print": {"command": "pause", "sequence_id": ""}}, "invalid_sequence_id"),
        (
            {"print": {"command": "pause", "sequence_id": "\N{ARABIC-INDIC DIGIT ONE}"}},
            "invalid_sequence_id",
        ),
        ({"print": {"command": "pause", "sequence_id": "1a"}}, "invalid_sequence_id"),
        ({"print": {"command": "pause", "sequence_id": "1" * 65}}, "invalid_sequence_id"),
    ],
)
def test_every_ambiguous_envelope_is_denied(payload: object, code: str) -> None:
    assert decode_grove_request(payload) == CommandDenial(code=code)


@given(st.recursive(st.none() | st.booleans() | st.integers() | st.text(), st.lists))
def test_non_mapping_payloads_never_raise_or_decode(payload: object) -> None:
    if isinstance(payload, dict):
        return
    assert isinstance(decode_grove_request(payload), CommandDenial)
