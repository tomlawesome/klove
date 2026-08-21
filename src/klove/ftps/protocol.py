"""Strict, pure protocol state for Grove's observed implicit-FTPS flow."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Final, Never

from klove.errors import ProtocolError

_MAX_LINE_BYTES: Final = 512
_ACCESS_CODE: Final = re.compile(r"[A-Za-z0-9_-]{20}", re.ASCII)
_RESERVED_PATH: Final = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._-]*\.3mf", re.ASCII)


class FtpsProtocolError(ProtocolError):
    """The peer violated the bounded FTPS command contract."""


class FtpsAction(Enum):
    """A validated protocol action for an outer authenticated TLS adapter."""

    USERNAME_ACCEPTED = auto()
    PASSWORD_PRESENTED = auto()
    PROTECTION_BUFFER_SET = auto()
    PRIVATE_DATA_PROTECTION_SET = auto()
    DELETE_REQUESTED = auto()
    PASSIVE_ENDPOINT_REQUESTED = auto()
    UPLOAD_REQUESTED = auto()
    DATA_TRANSFER_COMPLETED = auto()
    SESSION_CLOSED = auto()


@dataclass(frozen=True, slots=True)
class FtpsEvent:
    """One validated action; its argument is deliberately omitted from repr."""

    action: FtpsAction
    argument: str | None = field(default=None, repr=False)


class _State(Enum):
    EXPECT_USER = auto()
    EXPECT_PASS = auto()
    EXPECT_PBSZ = auto()
    EXPECT_PROT = auto()
    EXPECT_OPERATION = auto()
    EXPECT_STOR = auto()
    EXPECT_DATA_COMPLETION = auto()
    EXPECT_QUIT = auto()
    CLOSED = auto()
    REJECTED = auto()


class FtpsProtocolSession:
    """Consume exactly one observed Grove upload or cleanup command sequence.

    This object performs no authentication, TLS, socket, filesystem, or staging
    work. The adapter supplies an already reserved exact path and reports data
    channel properties after independently establishing the protected channel.
    Any violation permanently rejects the session.
    """

    def __init__(self, authorized_path: str | Callable[[str], object]) -> None:
        self._path_authorizer: Callable[[str], object]
        if type(authorized_path) is str:
            if _RESERVED_PATH.fullmatch(authorized_path) is None:
                raise FtpsProtocolError("invalid authorized FTPS path")
            self._path_authorizer = lambda candidate: candidate == authorized_path
        elif callable(authorized_path):
            self._path_authorizer = authorized_path
        else:
            raise FtpsProtocolError("invalid authorized FTPS path")
        self._selected_path: str | None = None
        self._state = _State.EXPECT_USER

    @property
    def closed(self) -> bool:
        """Return whether the peer completed the exact sequence with QUIT."""
        return self._state is _State.CLOSED

    def receive_line(self, line: bytes) -> FtpsEvent:
        """Validate one complete command line including its required CRLF."""
        if self._state in {_State.CLOSED, _State.REJECTED}:
            self._reject()
        command, argument = self._parse_line(line)
        handlers: dict[_State, Callable[[str, str | None], FtpsEvent]] = {
            _State.EXPECT_USER: self._receive_user,
            _State.EXPECT_PASS: self._receive_pass,
            _State.EXPECT_PBSZ: self._receive_pbsz,
            _State.EXPECT_PROT: self._receive_prot,
            _State.EXPECT_OPERATION: self._receive_operation,
            _State.EXPECT_STOR: self._receive_stor,
            _State.EXPECT_DATA_COMPLETION: self._reject_command,
            _State.EXPECT_QUIT: self._receive_quit,
        }
        return handlers[self._state](command, argument)

    def _parse_line(self, line: bytes) -> tuple[str, str | None]:
        if type(line) is not bytes or len(line) > _MAX_LINE_BYTES or not line.endswith(b"\r\n"):
            self._reject()
        payload = line[:-2]
        if not payload or b"\r" in payload or b"\n" in payload:
            self._reject()
        try:
            command_line = payload.decode("ascii")
        except UnicodeDecodeError:
            self._reject()

        if " " in command_line:
            command, argument = command_line.split(" ", 1)
            if not command or not argument or " " in argument or "\t" in argument:
                self._reject()
        else:
            command, argument = command_line, None
            if "\t" in command:
                self._reject()
        return command, argument

    def _receive_user(self, command: str, argument: str | None) -> FtpsEvent:
        if command != "USER" or argument != "bblp":
            self._reject()
        self._state = _State.EXPECT_PASS
        return FtpsEvent(FtpsAction.USERNAME_ACCEPTED, argument)

    def _receive_pass(self, command: str, argument: str | None) -> FtpsEvent:
        if command != "PASS" or argument is None or _ACCESS_CODE.fullmatch(argument) is None:
            self._reject()
        self._state = _State.EXPECT_PBSZ
        return FtpsEvent(FtpsAction.PASSWORD_PRESENTED, argument)

    def _receive_pbsz(self, command: str, argument: str | None) -> FtpsEvent:
        if command != "PBSZ" or argument != "0":
            self._reject()
        self._state = _State.EXPECT_PROT
        return FtpsEvent(FtpsAction.PROTECTION_BUFFER_SET)

    def _receive_prot(self, command: str, argument: str | None) -> FtpsEvent:
        if command != "PROT" or argument != "P":
            self._reject()
        self._state = _State.EXPECT_OPERATION
        return FtpsEvent(FtpsAction.PRIVATE_DATA_PROTECTION_SET)

    def _receive_operation(self, command: str, argument: str | None) -> FtpsEvent:
        if command == "DELE" and self._authorize_path(argument):
            self._selected_path = argument
            self._state = _State.EXPECT_QUIT
            return FtpsEvent(FtpsAction.DELETE_REQUESTED, argument)
        if command == "PASV" and argument is None:
            self._state = _State.EXPECT_STOR
            return FtpsEvent(FtpsAction.PASSIVE_ENDPOINT_REQUESTED)
        self._reject()

    def _receive_stor(self, command: str, argument: str | None) -> FtpsEvent:
        if command != "STOR" or not self._authorize_path(argument):
            self._reject()
        self._selected_path = argument
        self._state = _State.EXPECT_DATA_COMPLETION
        return FtpsEvent(FtpsAction.UPLOAD_REQUESTED, argument)

    def _receive_quit(self, command: str, argument: str | None) -> FtpsEvent:
        if command != "QUIT" or argument is not None:
            self._reject()
        self._state = _State.CLOSED
        return FtpsEvent(FtpsAction.SESSION_CLOSED)

    def _reject_command(self, _command: str, _argument: str | None) -> FtpsEvent:
        self._reject()

    def complete_data_transfer(self, *, protected: bool, same_peer: bool) -> FtpsEvent:
        """Accept exactly one completed protected data channel from the control peer."""
        if (
            self._state is not _State.EXPECT_DATA_COMPLETION
            or protected is not True
            or same_peer is not True
        ):
            self._reject()
        if self._selected_path is None:
            self._reject()
        self._state = _State.EXPECT_QUIT
        return FtpsEvent(FtpsAction.DATA_TRANSFER_COMPLETED, self._selected_path)

    def _authorize_path(self, value: str | None) -> bool:
        if value is None or _RESERVED_PATH.fullmatch(value) is None:
            return False
        try:
            return self._path_authorizer(value) is True
        except Exception:
            return False

    def _reject(self) -> Never:
        self._state = _State.REJECTED
        raise FtpsProtocolError("FTPS command sequence rejected")
