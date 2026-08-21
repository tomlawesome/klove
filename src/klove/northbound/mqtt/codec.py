"""Bounded MQTT 3.1.1 packet codec with no socket or broker behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

MAX_PACKET_BYTES: Final = 64 * 1024
MAX_WIRE_BYTES: Final = MAX_PACKET_BYTES + 5
MAX_CLIENT_ID_BYTES: Final = 128
MAX_CREDENTIAL_BYTES: Final = 64
MAX_TOPIC_BYTES: Final = 128
MAX_REPORT_BYTES: Final = 8 * 1024
MAX_DECIMAL_COMPONENT_DIGITS: Final = 20
_ACCESS_CODE_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


class MqttCodecError(ValueError):
    """Stable non-reflective decoding error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class MqttCredentials:
    """Credentials whose representation never contains the access code."""

    username: str
    _password: str = field(repr=False)

    @property
    def password(self) -> str:
        """Return the password only to a future owner-only authentication boundary."""
        return self._password


@dataclass(frozen=True, slots=True)
class ConnectPacket:
    client_id: str
    serial: str
    printer_id: int
    session_counter: int
    keepalive_seconds: int
    credentials: MqttCredentials


@dataclass(frozen=True, slots=True)
class SubscribePacket:
    packet_id: int
    topics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishPacket:
    topic: str
    packet_id: int
    duplicate: bool
    _payload: bytes = field(repr=False)

    @property
    def payload(self) -> bytes:
        """Return bounded opaque bytes only to a future exact payload decoder."""
        return self._payload


@dataclass(frozen=True, slots=True)
class PubackPacket:
    packet_id: int


@dataclass(frozen=True, slots=True)
class PingreqPacket:
    pass


@dataclass(frozen=True, slots=True)
class DisconnectPacket:
    pass


MqttPacket = (
    ConnectPacket
    | SubscribePacket
    | PublishPacket
    | PubackPacket
    | PingreqPacket
    | DisconnectPacket
)


class MqttPacketParser:
    """Incrementally decode bounded client packets and permanently fail closed."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._failed = False

    def feed(self, chunk: bytes) -> tuple[MqttPacket, ...]:
        """Consume one bounded byte chunk without allocating beyond the hard cap."""
        if self._failed:
            raise MqttCodecError("codec_failed")
        if type(chunk) is not bytes:
            self._fail("chunk_invalid")
        if len(chunk) > MAX_WIRE_BYTES - len(self._buffer):
            self._fail("packet_too_large")
        self._buffer.extend(chunk)
        decoded: list[MqttPacket] = []
        try:
            while True:
                frame = _next_frame(self._buffer)
                if frame is None:
                    break
                first, body, consumed = frame
                decoded.append(_decode_packet(first, body))
                del self._buffer[:consumed]
        except MqttCodecError:
            self._failed = True
            self._buffer.clear()
            raise
        return tuple(decoded)

    def _fail(self, code: str) -> None:
        self._failed = True
        self._buffer.clear()
        raise MqttCodecError(code)


def encode_connack() -> bytes:
    """Encode the only accepted successful MQTT 3.1.1 connection acknowledgement."""
    return b"\x20\x02\x00\x00"


def encode_suback(packet_id: int) -> bytes:
    """Grant only observed QoS 0 subscriptions."""
    return b"\x90\x03" + _packet_id(packet_id) + b"\x00"


def encode_puback(packet_id: int) -> bytes:
    """Acknowledge one QoS 1 publish identity."""
    return b"\x40\x02" + _packet_id(packet_id)


def encode_pingresp() -> bytes:
    """Encode the exact MQTT ping response."""
    return b"\xd0\x00"


def encode_report(serial: str, payload: bytes) -> bytes:
    """Encode one bounded non-retained QoS 0 report on a canonical serial topic."""
    if type(payload) is not bytes or len(payload) > MAX_REPORT_BYTES:
        raise MqttCodecError("report_invalid")
    topic = f"device/{_serial(serial)}/report".encode()
    body = len(topic).to_bytes(2, "big") + topic + payload
    return b"\x30" + _encode_remaining_length(len(body)) + body


def _next_frame(buffer: bytearray) -> tuple[int, bytes, int] | None:
    if len(buffer) < 2:
        return None
    decoded_length = _decode_remaining_length(buffer)
    if decoded_length is None:
        return None
    remaining, length_bytes = decoded_length
    total = 1 + length_bytes + remaining
    if len(buffer) < total:
        return None
    return buffer[0], bytes(buffer[1 + length_bytes : total]), total


def _decode_remaining_length(buffer: bytearray) -> tuple[int, int] | None:
    value = 0
    multiplier = 1
    for index in range(1, min(len(buffer), 5)):
        encoded = buffer[index]
        value += (encoded & 127) * multiplier
        if value > MAX_PACKET_BYTES:
            raise MqttCodecError("packet_too_large")
        if encoded & 128 == 0:
            length_bytes = index
            if _encode_remaining_length(value) != bytes(buffer[1 : index + 1]):
                raise MqttCodecError("remaining_length_invalid")
            return value, length_bytes
        multiplier *= 128
    if len(buffer) >= 5:
        raise MqttCodecError("remaining_length_invalid")
    return None


def _encode_remaining_length(value: int) -> bytes:
    if not 0 <= value <= MAX_PACKET_BYTES:
        raise MqttCodecError("remaining_length_invalid")
    encoded = bytearray()
    while True:
        digit = value % 128
        value //= 128
        encoded.append(digit | (128 if value else 0))
        if not value:
            return bytes(encoded)


def _decode_packet(first: int, body: bytes) -> MqttPacket:
    packet_type = first >> 4
    flags = first & 15
    if packet_type == 1 and flags == 0:
        return _decode_connect(body)
    if packet_type == 8 and flags == 2:
        return _decode_subscribe(body)
    if packet_type == 3:
        return _decode_publish(flags, body)
    if packet_type == 4 and flags == 0:
        return PubackPacket(packet_id=_body_packet_id(body))
    if packet_type == 12 and flags == 0 and not body:
        return PingreqPacket()
    if packet_type == 14 and flags == 0 and not body:
        return DisconnectPacket()
    raise MqttCodecError("packet_unsupported")


def _decode_connect(body: bytes) -> ConnectPacket:
    reader = _Reader(body)
    if reader.string(MAX_TOPIC_BYTES) != "MQTT" or reader.byte() != 4:
        raise MqttCodecError("connect_protocol_invalid")
    flags = reader.byte()
    if flags != 194:
        raise MqttCodecError("connect_flags_invalid")
    if reader.u16() != 30:
        raise MqttCodecError("connect_keepalive_invalid")
    client_id = reader.string(MAX_CLIENT_ID_BYTES)
    username = reader.string(MAX_CREDENTIAL_BYTES)
    password = reader.string(MAX_CREDENTIAL_BYTES)
    reader.finish()
    client_identity = _client_identity(client_id)
    if username != "bblp" or not _access_code(password):
        raise MqttCodecError("connect_credentials_invalid")
    return ConnectPacket(
        client_id=client_id,
        serial=client_identity[0],
        printer_id=client_identity[1],
        session_counter=client_identity[2],
        keepalive_seconds=30,
        credentials=MqttCredentials(username=username, _password=password),
    )


def _decode_subscribe(body: bytes) -> SubscribePacket:
    reader = _Reader(body)
    packet_id = reader.packet_id()
    topics: list[str] = []
    while reader.remaining:
        topic = reader.string(MAX_TOPIC_BYTES)
        if reader.byte() != 0 or not _client_topic(topic):
            raise MqttCodecError("subscription_invalid")
        topics.append(topic)
    if not _exact_subscription_topics(topics):
        raise MqttCodecError("subscription_invalid")
    return SubscribePacket(packet_id=packet_id, topics=tuple(topics))


def _decode_publish(flags: int, body: bytes) -> PublishPacket:
    if flags & 1 or (flags & 6) != 2:
        raise MqttCodecError("publish_flags_invalid")
    reader = _Reader(body)
    topic = reader.string(MAX_TOPIC_BYTES)
    if not _request_topic(topic):
        raise MqttCodecError("publish_topic_invalid")
    packet_id = reader.packet_id()
    payload = reader.rest(MAX_PACKET_BYTES)
    return PublishPacket(
        topic=topic, packet_id=packet_id, duplicate=bool(flags & 8), _payload=payload
    )


def _body_packet_id(body: bytes) -> int:
    reader = _Reader(body)
    packet_id = reader.packet_id()
    reader.finish()
    return packet_id


def _packet_id(value: int) -> bytes:
    if type(value) is not int or not 1 <= value <= 65535:
        raise MqttCodecError("packet_id_invalid")
    return value.to_bytes(2, "big")


def _client_topic(value: str) -> bool:
    parts = value.split("/")
    if len(parts) != 3 or parts[0] != "device" or parts[2] not in {"request", "report"}:
        return False
    try:
        return _serial(parts[1]) == parts[1]
    except MqttCodecError:
        return False


def _request_topic(value: str) -> bool:
    return _client_topic(value) and value.endswith("/request")


def _exact_subscription_topics(topics: list[str]) -> bool:
    if (
        len(topics) != 2
        or len(set(topics)) != 2
        or not all(_client_topic(topic) for topic in topics)
    ):
        return False
    first = topics[0].split("/")
    serial = first[1]
    return set(topics) == {f"device/{serial}/report", f"device/{serial}/request"}


def _client_identity(value: str) -> tuple[str, int, int]:
    prefix = "bambuddy_"
    if not value.startswith(prefix):
        raise MqttCodecError("connect_client_id_invalid")
    parts = value[len(prefix) :].split("_")
    if len(parts) != 3:
        raise MqttCodecError("connect_client_id_invalid")
    serial, printer_id, session_counter = parts
    try:
        return _serial(serial), _decimal(printer_id), _decimal(session_counter)
    except MqttCodecError:
        raise MqttCodecError("connect_client_id_invalid") from None


def _decimal(value: str) -> int:
    if (
        not 1 <= len(value) <= MAX_DECIMAL_COMPONENT_DIGITS
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise MqttCodecError("connect_client_id_invalid")
    return int(value)


def _access_code(value: str) -> bool:
    return len(value) == 20 and all(character in _ACCESS_CODE_CHARACTERS for character in value)


def _serial(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 42
        or not value.startswith("KLOVE-")
        or value[14] != "-"
        or value[19] != "-"
        or value[24] != "-"
        or value[29] != "-"
        or any(character not in "0123456789ABCDEF-" for character in value[len("KLOVE-") :])
    ):
        raise MqttCodecError("topic_invalid")
    return value


@dataclass(slots=True)
class _Reader:
    data: bytes
    offset: int = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def byte(self) -> int:
        if self.remaining < 1:
            raise MqttCodecError("packet_truncated")
        value = self.data[self.offset]
        self.offset += 1
        return value

    def u16(self) -> int:
        if self.remaining < 2:
            raise MqttCodecError("packet_truncated")
        value = int.from_bytes(self.data[self.offset : self.offset + 2], "big")
        self.offset += 2
        return value

    def packet_id(self) -> int:
        value = self.u16()
        _packet_id(value)
        return value

    def string(self, maximum: int) -> str:
        size = self.u16()
        if not 1 <= size <= maximum or self.remaining < size:
            raise MqttCodecError("string_invalid")
        raw = self.data[self.offset : self.offset + size]
        self.offset += size
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise MqttCodecError("string_invalid") from None
        if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise MqttCodecError("string_invalid")
        return value

    def rest(self, maximum: int) -> bytes:
        if self.remaining > maximum:
            raise MqttCodecError("packet_too_large")
        value = self.data[self.offset :]
        self.offset = len(self.data)
        return value

    def finish(self) -> None:
        if self.remaining:
            raise MqttCodecError("packet_trailing")
