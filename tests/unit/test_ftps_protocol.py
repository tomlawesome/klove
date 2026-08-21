from __future__ import annotations

import pytest

from klove.ftps.protocol import (
    FtpsAction,
    FtpsEvent,
    FtpsProtocolError,
    FtpsProtocolSession,
)

PATH = "/observation.3mf"
ACCESS_CODE = "A" * 20


def command(session: FtpsProtocolSession, value: str) -> FtpsEvent:
    return session.receive_line(value.encode("ascii") + b"\r\n")


def negotiate(session: FtpsProtocolSession) -> list[FtpsEvent]:
    return [
        command(session, "USER bblp"),
        command(session, f"PASS {ACCESS_CODE}"),
        command(session, "PBSZ 0"),
        command(session, "PROT P"),
    ]


def test_accepts_exact_protected_same_peer_upload_sequence() -> None:
    session = FtpsProtocolSession(PATH)

    events = negotiate(session)
    events.extend(
        [
            command(session, "PASV"),
            command(session, f"STOR {PATH}"),
            session.complete_data_transfer(protected=True, same_peer=True),
            command(session, "QUIT"),
        ]
    )

    assert [event.action for event in events] == [
        FtpsAction.USERNAME_ACCEPTED,
        FtpsAction.PASSWORD_PRESENTED,
        FtpsAction.PROTECTION_BUFFER_SET,
        FtpsAction.PRIVATE_DATA_PROTECTION_SET,
        FtpsAction.PASSIVE_ENDPOINT_REQUESTED,
        FtpsAction.UPLOAD_REQUESTED,
        FtpsAction.DATA_TRANSFER_COMPLETED,
        FtpsAction.SESSION_CLOSED,
    ]
    assert [event.argument for event in events] == [
        "bblp",
        ACCESS_CODE,
        None,
        None,
        None,
        PATH,
        PATH,
        None,
    ]
    assert session.closed
    assert ACCESS_CODE not in repr(events[1])


def test_accepts_exact_delete_cleanup_sequence() -> None:
    session = FtpsProtocolSession(PATH)
    negotiate(session)

    deleted = command(session, f"DELE {PATH}")
    closed = command(session, "QUIT")

    assert deleted == FtpsEvent(FtpsAction.DELETE_REQUESTED, PATH)
    assert closed == FtpsEvent(FtpsAction.SESSION_CLOSED)
    assert session.closed


def test_path_policy_can_authorize_one_canonical_name_after_login() -> None:
    checked: list[str] = []

    def authorize(path: str) -> bool:
        checked.append(path)
        return path == PATH

    session = FtpsProtocolSession(authorize)
    negotiate(session)

    assert command(session, f"DELE {PATH}") == FtpsEvent(FtpsAction.DELETE_REQUESTED, PATH)
    assert checked == [PATH]


@pytest.mark.parametrize("decision", [False, None, 1, RuntimeError("policy failure")])
def test_path_policy_denial_or_failure_permanently_rejects(decision: object) -> None:
    def authorize(_path: str) -> object:
        if isinstance(decision, Exception):
            raise decision
        return decision

    session = FtpsProtocolSession(authorize)
    negotiate(session)
    with pytest.raises(FtpsProtocolError):
        command(session, f"DELE {PATH}")
    with pytest.raises(FtpsProtocolError):
        command(session, "QUIT")


def test_path_policy_never_receives_an_unsafe_client_path() -> None:
    checked: list[str] = []

    def authorize(path: str) -> bool:
        checked.append(path)
        return True

    session = FtpsProtocolSession(authorize)
    negotiate(session)

    with pytest.raises(FtpsProtocolError):
        command(session, "DELE /../observation.3mf")

    assert checked == []


def test_missing_selected_path_defensively_rejects_data_completion() -> None:
    session = FtpsProtocolSession(PATH)
    negotiate(session)
    command(session, "PASV")
    command(session, f"STOR {PATH}")
    session._selected_path = None

    with pytest.raises(FtpsProtocolError):
        session.complete_data_transfer(protected=True, same_peer=True)


@pytest.mark.parametrize(
    "path",
    [
        "observation.3mf",
        "/.hidden.3mf",
        "/../observation.3mf",
        "/dir/observation.3mf",
        "/observation%2e3mf",
        "/observation.gcode",
        "/observation 1.3mf",
        "/observation.3mf/",
        "//observation.3mf",
        "/observé.3mf",
        1,
    ],
)
def test_rejects_unsafe_authorized_paths(path: object) -> None:
    with pytest.raises(FtpsProtocolError, match="invalid authorized FTPS path"):
        FtpsProtocolSession(path)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"USER bblp\n",
        b"USER bblp\r",
        b"USER bblp",
        b"USER bblp\r\nextra\r\n",
        b"USER\tbblp\r\n",
        b"USER  bblp\r\n",
        b" USER bblp\r\n",
        b"USER bblp \r\n",
        b"USER b\tblp\r\n",
        b"US\xffR bblp\r\n",
        b"\r\n",
        b"X" * 511 + b"\r\n",
        "USER bblp\r\n",
    ],
)
def test_rejects_malformed_or_unbounded_lines(line: object) -> None:
    session = FtpsProtocolSession(PATH)
    with pytest.raises(FtpsProtocolError, match="command sequence rejected"):
        session.receive_line(line)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "first", ["user bblp", "USER BBLP", f"PASS {ACCESS_CODE}", "USER", "USER x"]
)
def test_rejects_wrong_initial_command(first: str) -> None:
    session = FtpsProtocolSession(PATH)
    with pytest.raises(FtpsProtocolError):
        command(session, first)


@pytest.mark.parametrize(
    "password_command",
    [
        "USER bblp",
        "PASS",
        "PASS bad value",
        "PASS bad\tvalue",
        f"pass {ACCESS_CODE}",
        "PASS short",
        "PASS " + "x" * 21,
        "PASS " + "x" * 19,
        "PASS " + "x" * 19 + "!",
    ],
)
def test_rejects_invalid_password_step(password_command: str) -> None:
    session = FtpsProtocolSession(PATH)
    command(session, "USER bblp")
    with pytest.raises(FtpsProtocolError):
        command(session, password_command)


def test_rejects_password_control_character() -> None:
    session = FtpsProtocolSession(PATH)
    command(session, "USER bblp")
    with pytest.raises(FtpsProtocolError):
        session.receive_line(b"PASS bad\x1fvalue\r\n")


@pytest.mark.parametrize("value", ["PBSZ 1", "PBSZ", "pbsz 0", "PROT P", "PBSZ +0"])
def test_rejects_wrong_pbsz(value: str) -> None:
    session = FtpsProtocolSession(PATH)
    command(session, "USER bblp")
    command(session, f"PASS {ACCESS_CODE}")
    with pytest.raises(FtpsProtocolError):
        command(session, value)


@pytest.mark.parametrize("value", ["PROT C", "PROT", "prot P", "PBSZ 0", "PROT S"])
def test_rejects_non_private_or_wrong_protection(value: str) -> None:
    session = FtpsProtocolSession(PATH)
    command(session, "USER bblp")
    command(session, f"PASS {ACCESS_CODE}")
    command(session, "PBSZ 0")
    with pytest.raises(FtpsProtocolError):
        command(session, value)


@pytest.mark.parametrize(
    "value",
    [
        "PORT 127,0,0,1,1,1",
        "EPRT |1|127.0.0.1|1234|",
        "RETR /observation.3mf",
        "LIST",
        "NLST",
        "CWD /",
        "PWD",
        "TYPE I",
        "QUIT",
        "PASV extra",
        "DELE /other.3mf",
        "DELE /../observation.3mf",
        "DELE /dir/observation.3mf",
        "DELE /observation%2e3mf",
    ],
)
def test_rejects_unobserved_operation_or_non_exact_delete(value: str) -> None:
    session = FtpsProtocolSession(PATH)
    negotiate(session)
    with pytest.raises(FtpsProtocolError):
        command(session, value)


@pytest.mark.parametrize(
    "value",
    [
        "STOR /other.3mf",
        "STOR /../observation.3mf",
        "STOR /dir/observation.3mf",
        "STOR /observation%2e3mf",
        "STOR",
        "PASV",
        "QUIT",
    ],
)
def test_rejects_non_exact_or_out_of_order_upload(value: str) -> None:
    session = FtpsProtocolSession(PATH)
    negotiate(session)
    command(session, "PASV")
    with pytest.raises(FtpsProtocolError):
        command(session, value)


@pytest.mark.parametrize(
    ("protected", "same_peer"),
    [(False, True), (True, False), (False, False), (1, True), (True, 1)],
)
def test_rejects_unprotected_or_different_peer_data(protected: object, same_peer: object) -> None:
    session = FtpsProtocolSession(PATH)
    negotiate(session)
    command(session, "PASV")
    command(session, f"STOR {PATH}")
    with pytest.raises(FtpsProtocolError):
        session.complete_data_transfer(
            protected=protected,  # type: ignore[arg-type]
            same_peer=same_peer,  # type: ignore[arg-type]
        )


def test_rejects_command_while_data_is_pending_and_completion_in_other_states() -> None:
    session = FtpsProtocolSession(PATH)
    with pytest.raises(FtpsProtocolError):
        session.complete_data_transfer(protected=True, same_peer=True)

    session = FtpsProtocolSession(PATH)
    negotiate(session)
    command(session, "PASV")
    command(session, f"STOR {PATH}")
    with pytest.raises(FtpsProtocolError):
        command(session, "QUIT")


@pytest.mark.parametrize("value", ["QUIT extra", "quit", "PASV", f"DELE {PATH}"])
def test_rejects_wrong_final_command(value: str) -> None:
    session = FtpsProtocolSession(PATH)
    negotiate(session)
    command(session, f"DELE {PATH}")
    with pytest.raises(FtpsProtocolError):
        command(session, value)


def test_rejects_repeats_after_data_completion_or_close_and_stays_poisoned() -> None:
    session = FtpsProtocolSession(PATH)
    negotiate(session)
    command(session, "PASV")
    command(session, f"STOR {PATH}")
    session.complete_data_transfer(protected=True, same_peer=True)
    with pytest.raises(FtpsProtocolError):
        session.complete_data_transfer(protected=True, same_peer=True)
    with pytest.raises(FtpsProtocolError):
        command(session, "QUIT")
    assert not session.closed

    closed = FtpsProtocolSession(PATH)
    negotiate(closed)
    command(closed, f"DELE {PATH}")
    command(closed, "QUIT")
    with pytest.raises(FtpsProtocolError):
        command(closed, "QUIT")
    assert not closed.closed
