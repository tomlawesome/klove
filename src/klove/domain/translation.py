"""Strict Grove command decoding with no actuation side effects."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CommandKind(StrEnum):
    """Canonical operations recognized by the first protocol contract."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


class DecodedCommand(BaseModel):
    """A syntactically valid command that still requires authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: Literal[True] = True
    kind: CommandKind
    sequence_id: str
    payload_hash: str


class CommandDenial(BaseModel):
    """A stable denial that never reflects hostile input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: Literal[False] = False
    code: str


DecodeResult = DecodedCommand | CommandDenial

_COMMANDS = {
    "pause": CommandKind.PAUSE,
    "resume": CommandKind.RESUME,
    "stop": CommandKind.CANCEL,
}
_ALLOWED_FIELDS = frozenset({"command", "sequence_id"})


def decode_grove_request(payload: object) -> DecodeResult:
    """Decode only the exact non-parameterized Grove job-control envelope."""
    if not isinstance(payload, dict) or set(payload) != {"print"}:
        return CommandDenial(code="invalid_envelope")
    print_block = payload["print"]
    if not isinstance(print_block, dict):
        return CommandDenial(code="invalid_print_block")
    if set(print_block) != _ALLOWED_FIELDS:
        return CommandDenial(code="unexpected_fields")
    command = print_block.get("command")
    if not isinstance(command, str) or command not in _COMMANDS:
        return CommandDenial(code="unsupported_command")
    sequence_id = print_block.get("sequence_id")
    if (
        not isinstance(sequence_id, str)
        or not sequence_id.isascii()
        or not sequence_id.isdecimal()
        or not 1 <= len(sequence_id) <= 64
    ):
        return CommandDenial(code="invalid_sequence_id")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return DecodedCommand(
        kind=_COMMANDS[command],
        sequence_id=sequence_id,
        payload_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def deny_actuation(_command: DecodedCommand) -> CommandDenial:
    """Keep the first implementation slice physically incapable of actuation."""
    return CommandDenial(code="actuation_disabled")
