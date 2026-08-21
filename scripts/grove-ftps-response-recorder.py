#!/usr/bin/env python3
"""Bounded implicit-FTPS server for sanitized Grove client observations."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import threading
from pathlib import Path

CONTROL_PORT = 990
MAX_LINE_BYTES = 512
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
EXPECTED_PATH = "/observation.3mf"
EXPECTED_USERNAME = "bblp"
EXPECTED_PASSWORD = "TEST0000"  # noqa: S105 -- fixed fake observation-only credential.
OBSERVATION_SERIAL = "FTPSOBS0000001"


class ObservationFailure(Exception):
    """A bounded recorder failure without client-controlled text."""


def _reply(connection: ssl.SSLSocket, code: int, text: str) -> None:
    connection.sendall(f"{code} {text}\r\n".encode("ascii"))


def _read_command(connection: ssl.SSLSocket) -> tuple[str, str | None]:
    data = bytearray()
    while not data.endswith(b"\r\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise ObservationFailure("control_disconnected")
        data.extend(chunk)
        if len(data) > MAX_LINE_BYTES:
            raise ObservationFailure("command_too_long")
    try:
        line = bytes(data[:-2]).decode("ascii")
    except UnicodeDecodeError as exc:
        raise ObservationFailure("command_not_ascii") from exc
    command, separator, argument = line.partition(" ")
    if not command or not command.isupper():
        raise ObservationFailure("command_invalid")
    return command, argument if separator else None


def _expect(
    connection: ssl.SSLSocket,
    expected_command: str,
    expected_argument: str | None,
) -> None:
    command, argument = _read_command(connection)
    if command != expected_command or argument != expected_argument:
        raise ObservationFailure(f"unexpected_{expected_command.lower()}")


def _login(connection: ssl.SSLSocket) -> list[dict[str, object]]:
    replies: list[dict[str, object]] = [
        {"event": "implicit_tls_greeting", "code": 220, "category": "service_ready"}
    ]
    _reply(connection, 220, "Observation service ready")
    _expect(connection, "USER", EXPECTED_USERNAME)
    _reply(connection, 331, "Password required")
    replies.append({"event": "user", "code": 331, "category": "password_required"})
    command, password = _read_command(connection)
    if command != "PASS" or password != EXPECTED_PASSWORD:
        _reply(connection, 530, "Authentication failed")
        raise ObservationFailure("authentication_failed")
    _reply(connection, 230, "Login successful")
    replies.append({"event": "pass", "code": 230, "category": "login_successful"})
    _expect(connection, "PBSZ", "0")
    _reply(connection, 200, "Protection buffer accepted")
    replies.append({"event": "pbsz", "code": 200, "category": "command_successful"})
    _expect(connection, "PROT", "P")
    _reply(connection, 200, "Private data protection accepted")
    replies.append({"event": "prot", "code": 200, "category": "command_successful"})
    return replies


def _control_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(
        "/tmp/observation-cert.pem",  # noqa: S108 -- private container tmpfs.
        "/tmp/observation-key.pem",  # noqa: S108 -- private container tmpfs.
    )
    return context


def _mqtt_read_exact(connection: ssl.SSLSocket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise OSError("mqtt_disconnected")
        result.extend(chunk)
    return bytes(result)


def _mqtt_read_packet(connection: ssl.SSLSocket) -> tuple[int, bytes]:
    header = _mqtt_read_exact(connection, 1)[0]
    multiplier = 1
    remaining = 0
    for _index in range(4):
        encoded = _mqtt_read_exact(connection, 1)[0]
        remaining += (encoded & 127) * multiplier
        if remaining > 1024 * 1024:
            raise OSError("mqtt_packet_too_large")
        if encoded & 128 == 0:
            return header, _mqtt_read_exact(connection, remaining)
        multiplier *= 128
    raise OSError("mqtt_remaining_length_invalid")


def _mqtt_encode_remaining(length: int) -> bytes:
    result = bytearray()
    while True:
        encoded = length % 128
        length //= 128
        if length:
            encoded |= 128
        result.append(encoded)
        if not length:
            return bytes(result)


def _mqtt_publish_status(connection: ssl.SSLSocket) -> None:
    topic = f"device/{OBSERVATION_SERIAL}/report".encode("ascii")
    payload = json.dumps(
        {
            "print": {
                "command": "push_status",
                "sequence_id": "1",
                "msg": 0,
                "gcode_state": "IDLE",
                "subtask_name": "",
                "mc_percent": 0,
                "mc_remaining_time": 0,
            }
        },
        separators=(",", ":"),
    ).encode("ascii")
    body = len(topic).to_bytes(2, "big") + topic + payload
    connection.sendall(b"\x30" + _mqtt_encode_remaining(len(body)) + body)


def _mqtt_subscription_count(body: bytes) -> tuple[bytes, int]:
    if len(body) < 2:
        raise OSError("mqtt_subscribe_invalid")
    packet_id = body[:2]
    cursor = 2
    count = 0
    while cursor < len(body):
        if cursor + 2 > len(body):
            raise OSError("mqtt_subscribe_invalid")
        topic_size = int.from_bytes(body[cursor : cursor + 2], "big")
        cursor += 2 + topic_size
        if cursor >= len(body):
            raise OSError("mqtt_subscribe_invalid")
        cursor += 1
        count += 1
    return packet_id, count


def _mqtt_session(connection: ssl.SSLSocket) -> None:
    header, _body = _mqtt_read_packet(connection)
    if header != 0x10:
        raise OSError("mqtt_connect_missing")
    connection.sendall(b"\x20\x02\x00\x00")
    while True:
        header, body = _mqtt_read_packet(connection)
        packet_type = header >> 4
        if packet_type == 8:
            packet_id, subscriptions = _mqtt_subscription_count(body)
            response = packet_id + (b"\x00" * subscriptions)
            connection.sendall(b"\x90" + _mqtt_encode_remaining(len(response)) + response)
            _mqtt_publish_status(connection)
        elif packet_type == 3:
            qos = (header >> 1) & 3
            if len(body) < 2:
                raise OSError("mqtt_publish_invalid")
            topic_size = int.from_bytes(body[:2], "big")
            packet_id_offset = 2 + topic_size
            if qos == 1:
                if packet_id_offset + 2 > len(body):
                    raise OSError("mqtt_publish_invalid")
                connection.sendall(b"\x40\x02" + body[packet_id_offset : packet_id_offset + 2])
        elif packet_type == 12:
            connection.sendall(b"\xd0\x00")
        elif packet_type == 14:
            return


def _mqtt_broker(
    context: ssl.SSLContext,
    stop: threading.Event,
    ready: threading.Event,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 8883))  # noqa: S104 -- isolated internal Docker network.
        listener.listen(2)
        listener.settimeout(1)
        ready.set()
        while not stop.is_set():
            try:
                plain, _peer = listener.accept()
            except TimeoutError:
                continue
            plain.settimeout(40)
            try:
                with context.wrap_socket(plain, server_side=True) as protected:
                    _mqtt_session(protected)
            except (OSError, ssl.SSLError, TimeoutError):
                plain.close()


def _listen() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(
        ("0.0.0.0", CONTROL_PORT)  # noqa: S104 -- isolated internal Docker network.
    )
    listener.listen(2)
    listener.settimeout(45)
    return listener


def _accept_control(
    listener: socket.socket,
    context: ssl.SSLContext,
) -> tuple[ssl.SSLSocket, str, str]:
    plain, peer = listener.accept()
    plain.settimeout(15)
    local_address = plain.getsockname()[0]
    try:
        protected = context.wrap_socket(plain, server_side=True)
    except Exception:
        plain.close()
        raise
    protected.settimeout(15)
    return protected, peer[0], local_address


def _cleanup_session(
    listener: socket.socket,
    context: ssl.SSLContext,
) -> tuple[dict[str, object], str]:
    connection, peer, _local = _accept_control(listener, context)
    with connection:
        tls_version = connection.version()
        replies = _login(connection)
        _expect(connection, "DELE", EXPECTED_PATH)
        _reply(connection, 250, "Requested path cleanup accepted")
        replies.append({"event": "dele", "code": 250, "category": "path_action_successful"})
        _expect(connection, "QUIT", None)
        _reply(connection, 221, "Goodbye")
        replies.append({"event": "quit", "code": 221, "category": "service_closing"})
    return (
        {
            "purpose": "pre_upload_cleanup",
            "control_tls_version": tls_version,
            "server_replies": replies,
            "accepted_completion_order": ["dele_reply", "quit_reply", "control_close"],
        },
        peer,
    )


def _upload_session(  # noqa: PLR0915 -- linear wire transcript keeps ordering reviewable.
    listener: socket.socket,
    context: ssl.SSLContext,
    cleanup_peer: str,
) -> dict[str, object]:
    connection, peer, local_address = _accept_control(listener, context)
    passive: socket.socket | None = None
    with connection:
        tls_version = connection.version()
        replies = _login(connection)
        print("observation stage: upload_login_complete", flush=True)
        _expect(connection, "PASV", None)
        passive = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        passive.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        passive.bind(("0.0.0.0", 0))  # noqa: S104 -- isolated internal Docker network.
        passive.listen(1)
        passive.settimeout(15)
        passive_port = passive.getsockname()[1]
        octets = local_address.split(".")
        if len(octets) != 4 or any(not item.isdigit() for item in octets):
            raise ObservationFailure("passive_address_invalid")
        _reply(
            connection,
            227,
            "Entering Passive Mode "
            f"({','.join(octets)},{passive_port // 256},{passive_port % 256})",
        )
        replies.append({"event": "pasv", "code": 227, "category": "passive_endpoint_ready"})
        print("observation stage: passive_reply_sent", flush=True)
        _expect(connection, "STOR", EXPECTED_PATH)
        _reply(connection, 150, "Opening protected data connection")
        replies.append({"event": "stor_preliminary", "code": 150, "category": "transfer_starting"})
        print("observation stage: stor_preliminary_sent", flush=True)

        plain_data, data_peer = passive.accept()
        plain_data.settimeout(15)
        digest = hashlib.sha256()
        byte_count = 0
        data_close = "tls_eof"
        with context.wrap_socket(plain_data, server_side=True) as data:
            data_tls_version = data.version()
            print("observation stage: data_tls_complete", flush=True)
            while True:
                try:
                    chunk = data.recv(64 * 1024)
                except (ConnectionResetError, ssl.SSLEOFError):
                    if byte_count == 0:
                        raise
                    data_close = "transport_reset_after_payload"
                    break
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > MAX_UPLOAD_BYTES:
                    raise ObservationFailure("upload_too_large")
                digest.update(chunk)
        passive.close()
        passive = None
        _reply(connection, 226, "Transfer complete")
        replies.append({"event": "stor_complete", "code": 226, "category": "transfer_complete"})
        print("observation stage: stor_completion_sent", flush=True)
        _expect(connection, "QUIT", None)
        _reply(connection, 221, "Goodbye")
        replies.append({"event": "quit", "code": 221, "category": "service_closing"})

    if passive is not None:
        passive.close()
    return {
        "purpose": "protected_upload",
        "control_tls_version": tls_version,
        "server_replies": replies,
        "passive_endpoint": {
            "advertised_address_matches_control_local_address": True,
            "advertised_port_matches_open_listener": True,
            "data_peer_matches_control_peer": data_peer[0] == peer,
            "control_peer_matches_cleanup_session": peer == cleanup_peer,
        },
        "data_tls_version": data_tls_version,
        "data_channel_close": data_close,
        "upload": {
            "byte_count": byte_count,
            "sha256": f"sha256:{digest.hexdigest()}",
        },
        "accepted_completion_order": [
            "stor_preliminary_reply",
            "protected_data_close",
            "stor_completion_reply",
            "quit_reply",
            "control_close",
        ],
    }


def main() -> int:
    output = Path("/evidence/ftps-server-response-profile")
    context = _control_context()
    stop_mqtt = threading.Event()
    mqtt_ready = threading.Event()
    mqtt_thread = threading.Thread(
        target=_mqtt_broker,
        args=(context, stop_mqtt, mqtt_ready),
        daemon=True,
    )
    mqtt_thread.start()
    if not mqtt_ready.wait(timeout=5):
        raise ObservationFailure("mqtt_broker_not_ready")
    try:
        with _listen() as listener:
            try:
                cleanup, cleanup_peer = _cleanup_session(listener, context)
            except (OSError, ssl.SSLError, TimeoutError) as error:
                raise ObservationFailure(f"cleanup_{type(error).__name__}") from error
            try:
                print("observation stage: cleanup_complete", flush=True)
                upload = _upload_session(listener, context, cleanup_peer)
            except (OSError, ssl.SSLError, TimeoutError) as error:
                raise ObservationFailure(f"upload_{type(error).__name__}") from error
    finally:
        stop_mqtt.set()
        mqtt_thread.join(timeout=3)
    profile = {
        "profile_version": 1,
        "transport": "implicit-ftps",
        "upstream_revision": "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4",
        "observed": {
            "successful_client_flow": True,
            "sessions": [cleanup, upload],
        },
        "not_observed": [
            "client behavior for alternate FTP reply codes",
            "client behavior after control or data disconnect",
            "client retry timing after a wire failure",
            "host-published, NAT, or multi-address passive deployment",
            "multiple or concurrent transfer behavior",
        ],
    }
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile, indent=2) + "\n", encoding="ascii")
    os.replace(temporary, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ObservationFailure as error:
        print(f"observation failed: {error}")
        raise SystemExit(1) from None
    except (OSError, ssl.SSLError, TimeoutError) as error:
        print(f"observation failed: {type(error).__name__}")
        raise SystemExit(1) from None
