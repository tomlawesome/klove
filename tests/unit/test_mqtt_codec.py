from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from klove.northbound.mqtt.codec import (
    MAX_PACKET_BYTES,
    MAX_WIRE_BYTES,
    ConnectPacket,
    DisconnectPacket,
    MqttCodecError,
    MqttPacketParser,
    PingreqPacket,
    PubackPacket,
    PublishPacket,
    SubscribePacket,
    _encode_remaining_length,
    _Reader,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_report,
    encode_suback,
)

SERIAL = "KLOVE-01234567-89AB-CDEF-0123-456789ABCDEF"
OTHER_SERIAL = "KLOVE-FEDCBA98-7654-3210-FEDC-BA9876543210"
REQUEST = f"device/{SERIAL}/request"
REPORT = f"device/{SERIAL}/report"


def mqtt_string(value: str | bytes) -> bytes:
    encoded = value.encode() if isinstance(value, str) else value
    return len(encoded).to_bytes(2, "big") + encoded


def frame(first: int, body: bytes = b"") -> bytes:
    return bytes([first]) + _encode_remaining_length(len(body)) + body


def connect(  # noqa: PLR0913
    *,
    flags: int = 194,
    protocol: str = "MQTT",
    level: int = 4,
    keepalive: int = 30,
    client_id: str | bytes = f"bambuddy_{SERIAL}_1_1",
    username: str = "bblp",
    password: str = "X" * 20,
    trailing: bytes = b"",
) -> bytes:
    body = (
        mqtt_string(protocol)
        + bytes([level, flags])
        + keepalive.to_bytes(2, "big")
        + mqtt_string(client_id)
        + mqtt_string(username)
        + mqtt_string(password)
        + trailing
    )
    return frame(0x10, body)


def subscribe(topics: tuple[tuple[str, int], ...], packet_id: int = 2) -> bytes:
    body = packet_id.to_bytes(2, "big") + b"".join(
        mqtt_string(topic) + bytes([qos]) for topic, qos in topics
    )
    return frame(0x82, body)


def publish(
    *, flags: int = 2, topic: str = REQUEST, packet_id: int = 3, payload: bytes = b"{}"
) -> bytes:
    return frame(0x30 | flags, mqtt_string(topic) + packet_id.to_bytes(2, "big") + payload)


def test_incremental_parser_emits_only_typed_supported_packets() -> None:
    raw = b"".join(
        (
            connect(),
            subscribe(((REPORT, 0), (REQUEST, 0))),
            publish(),
            frame(0x40, b"\x00\x04"),
            frame(0xC0),
            frame(0xE0),
        )
    )
    parser = MqttPacketParser()
    packets = tuple(packet for byte in raw for packet in parser.feed(bytes([byte])))

    assert packets == (
        ConnectPacket(
            client_id=f"bambuddy_{SERIAL}_1_1",
            serial=SERIAL,
            printer_id=1,
            session_counter=1,
            keepalive_seconds=30,
            credentials=packets[0].credentials,  # type: ignore[union-attr]
        ),
        SubscribePacket(packet_id=2, topics=(REPORT, REQUEST)),
        PublishPacket(topic=REQUEST, packet_id=3, duplicate=False, _payload=b"{}"),
        PubackPacket(packet_id=4),
        PingreqPacket(),
        DisconnectPacket(),
    )
    assert isinstance(packets[0], ConnectPacket)
    assert packets[0].credentials.username == "bblp"
    assert packets[0].credentials.password == "X" * 20
    assert "X" * 20 not in repr(packets[0].credentials)


def test_duplicate_publish_is_typed_and_payload_is_not_in_its_representation() -> None:
    packet = MqttPacketParser().feed(publish(flags=10, payload=b"hostile"))[0]

    assert packet == PublishPacket(topic=REQUEST, packet_id=3, duplicate=True, _payload=b"hostile")
    assert isinstance(packet, PublishPacket)
    assert packet.payload == b"hostile"
    assert b"hostile".decode() not in repr(packet)


def test_exact_server_encoders_are_bounded() -> None:
    assert encode_connack() == b"\x20\x02\x00\x00"
    assert encode_suback(7) == b"\x90\x03\x00\x07\x00"
    assert encode_puback(7) == b"\x40\x02\x00\x07"
    assert encode_pingresp() == b"\xd0\x00"
    assert encode_report(SERIAL, b"ok") == frame(0x30, mqtt_string(REPORT) + b"ok")


@pytest.mark.parametrize(
    "raw",
    [
        b"\x50\x00",
        b"\xc0\x80\x00",
        b"\x30\x80\x80\x80\x04",
        b"\xc0\x80\x80\x80\x80",
        b"\xc0\x01\x00",
        b"\xe1\x00",
        frame(0x40, b"\x00\x00"),
        connect(protocol="MQIsdp"),
        connect(level=5),
        connect(flags=0),
        connect(keepalive=31),
        connect(username="guest"),
        connect(trailing=b"x"),
        connect(client_id=b"\xff"),
        connect(client_id="\x00"),
        connect(client_id="other"),
        connect(client_id="bambuddy_KLOVE-AB12_1_1"),
        connect(client_id=f"bambuddy_{SERIAL}_01_1"),
        connect(client_id=f"bambuddy_{SERIAL}_1_" + "1" * 21),
        connect(client_id=f"bambuddy_{SERIAL}_1_1_extra"),
        connect(password="s" * 5),
        connect(password="." * 20),
        subscribe(((REQUEST, 1),)),
        subscribe(((REQUEST, 0),)),
        subscribe(((f"device/{OTHER_SERIAL}/report", 0), (REQUEST, 0))),
        subscribe((("device/KLOVE-AB12/request", 0), (REPORT, 0))),
        subscribe((("device/KLOVE-AB12/#", 0), (REQUEST, 0))),
        subscribe(((REQUEST, 0), (REQUEST, 0))),
        publish(flags=1),
        publish(flags=4),
        publish(topic=REPORT),
        publish(topic=REPORT + "/extra"),
        publish(packet_id=0),
    ],
)
def test_malformed_or_unsupported_packets_fail_closed(raw: bytes) -> None:
    parser = MqttPacketParser()

    with pytest.raises(MqttCodecError) as raised:
        parser.feed(raw)

    assert raised.value.code in {
        "connect_credentials_invalid",
        "connect_client_id_invalid",
        "connect_flags_invalid",
        "connect_keepalive_invalid",
        "connect_protocol_invalid",
        "packet_id_invalid",
        "packet_unsupported",
        "packet_too_large",
        "packet_trailing",
        "publish_flags_invalid",
        "publish_topic_invalid",
        "remaining_length_invalid",
        "string_invalid",
        "subscription_invalid",
    }
    with pytest.raises(MqttCodecError, match="codec_failed"):
        parser.feed(b"")


@pytest.mark.parametrize("chunk", [bytearray(b"x"), b"x" * (MAX_WIRE_BYTES + 1)])
def test_parser_rejects_invalid_or_unbounded_chunks(chunk: object) -> None:
    with pytest.raises(MqttCodecError) as raised:
        MqttPacketParser().feed(chunk)  # type: ignore[arg-type]

    assert raised.value.code in {"chunk_invalid", "packet_too_large"}


@pytest.mark.parametrize("value", [0, -1, 65536, True, "1"])
def test_encoders_reject_invalid_packet_ids(value: object) -> None:
    with pytest.raises(MqttCodecError, match="packet_id_invalid"):
        encode_puback(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("serial", "payload"),
    [
        ("lowercase", b""),
        ("A" * 51, b""),
        (SERIAL, bytearray(b"x")),
        (SERIAL, b"x" * 8193),
    ],
)
def test_report_encoder_rejects_invalid_inputs(serial: str, payload: object) -> None:
    with pytest.raises(MqttCodecError) as raised:
        encode_report(serial, payload)  # type: ignore[arg-type]

    assert raised.value.code in {"report_invalid", "topic_invalid"}


def test_reader_rejects_truncation_and_oversized_rest() -> None:
    with pytest.raises(MqttCodecError, match="packet_truncated"):
        _Reader(b"").byte()
    with pytest.raises(MqttCodecError, match="packet_truncated"):
        _Reader(b"x").u16()
    with pytest.raises(MqttCodecError, match="string_invalid"):
        _Reader(b"\x00\x00").string(1)
    with pytest.raises(MqttCodecError, match="packet_too_large"):
        _Reader(b"x").rest(0)
    with pytest.raises(MqttCodecError, match="packet_trailing"):
        _Reader(b"x").finish()


@pytest.mark.parametrize("value", [-1, MAX_PACKET_BYTES + 1])
def test_remaining_length_encoder_rejects_invalid_values(value: int) -> None:
    with pytest.raises(MqttCodecError, match="remaining_length_invalid"):
        _encode_remaining_length(value)


@given(st.binary(max_size=512))
def test_arbitrary_bounded_bytes_never_emit_unsupported_values(raw: bytes) -> None:
    parser = MqttPacketParser()
    try:
        packets = parser.feed(raw)
    except MqttCodecError:
        return
    assert all(
        isinstance(
            packet,
            ConnectPacket
            | SubscribePacket
            | PublishPacket
            | PubackPacket
            | PingreqPacket
            | DisconnectPacket,
        )
        for packet in packets
    )
