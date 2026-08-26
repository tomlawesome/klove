"""Private-network black-box probe for the production FTPS container."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import stat
import time
from pathlib import Path
from uuid import UUID

HOST = socket.gethostbyname("klove")
CERTIFICATE = Path("/trust/tls-certificate.pem")
STAGING = Path("/staging")
PRINTER_UUID = "11111111-1111-4111-8111-111111111111"
CLIENT_PATH = "/contract.3mf"
PAYLOAD = b"KLOVE-FTPS-CONTRACT\x00\xff\r\n" * 257
PASSIVE_MIN = 50000
PASSIVE_MAX = 50009
_PASV = re.compile(r"Entering Passive Mode \((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)")
_ZERO_CAPABILITIES = {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}


def confinement() -> dict[str, object]:
    if os.getuid() != 10001 or os.getgid() != 10001:
        raise RuntimeError("contract peer identity mismatch")
    status = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            status[name] = value.strip()
    if any(status.get(name) != "0000000000000000" for name in _ZERO_CAPABILITIES):
        raise RuntimeError("contract peer retained a capability")
    if status.get("NoNewPrivs") != "1":
        raise RuntimeError("contract peer could gain privileges")
    return {
        "capabilities": "zero",
        "gid": os.getgid(),
        "no_new_privileges": True,
        "uid": os.getuid(),
    }


def readiness() -> tuple[int, ...]:
    observed: list[int] = []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection(HOST, 8080, timeout=0.2)
            connection.request("GET", "/health/ready")
            response = connection.getresponse()
            response.read(1024)
            observed.append(response.status)
            connection.close()
            if response.status == 200:
                break
        except OSError:
            pass
    if 503 not in observed or not observed or observed[-1] != 200:
        raise RuntimeError("readiness did not transition from 503 to 200")
    return tuple(observed)


def reply(stream: ssl.SSLSocket, expected: int, message: str | None = None) -> str:
    line = bytearray()
    while not line.endswith(b"\r\n"):
        chunk = stream.recv(1)
        if not chunk or len(line) >= 512:
            raise RuntimeError("bounded FTPS reply failed")
        line.extend(chunk)
    if int(line[:3]) != expected or line[3:4] != b" ":
        raise RuntimeError("unexpected FTPS reply")
    actual = line[4:-2].decode("ascii")
    if message is not None and actual != message:
        raise RuntimeError("FTPS reply text mismatch")
    return actual


def command(stream: ssl.SSLSocket, line: str, expected: int, message: str) -> None:
    encoded = line.encode("ascii") + b"\r\n"
    if len(encoded) > 512:
        raise RuntimeError("FTPS command exceeded bound")
    stream.sendall(encoded)
    reply(stream, expected, message)


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(CERTIFICATE))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def access_code() -> str:
    material = hashlib.sha256(CERTIFICATE.read_bytes()).digest()[:15]
    return base64.urlsafe_b64encode(material).decode("ascii")


def control(context: ssl.SSLContext) -> ssl.SSLSocket:
    raw = socket.create_connection((HOST, 990), 2)
    try:
        secured = context.wrap_socket(raw, server_hostname=HOST)
    except BaseException:
        raw.close()
        raise
    secured.settimeout(3)
    if secured.version() != "TLSv1.3":
        secured.close()
        raise RuntimeError("control TLS version mismatch")
    reply(secured, 220, "Klove ready")
    return secured


def authenticate(stream: ssl.SSLSocket) -> None:
    command(stream, "USER bblp", 331, "Password required")
    command(stream, f"PASS {access_code()}", 230, "Authenticated")
    command(stream, "PBSZ 0", 200, "Accepted")
    command(stream, "PROT P", 200, "Accepted")


def cleanup(context: ssl.SSLContext) -> list[int]:
    with control(context) as stream:
        authenticate(stream)
        command(stream, f"DELE {CLIENT_PATH}", 250, "Accepted")
        command(stream, "QUIT", 221, "Goodbye")
    return [220, 331, 230, 200, 200, 250, 221]


def passive_endpoint(message: str) -> tuple[str, int]:
    match = _PASV.fullmatch(message)
    if match is None:
        raise RuntimeError("PASV reply shape mismatch")
    octets = tuple(int(value) for value in match.groups())
    if any(value > 255 for value in octets):
        raise RuntimeError("PASV reply value out of range")
    address = ".".join(str(value) for value in octets[:4])
    port = octets[4] * 256 + octets[5]
    if address != HOST or not PASSIVE_MIN <= port <= PASSIVE_MAX:
        raise RuntimeError("PASV endpoint escaped the exact private contract")
    return address, port


def upload(context: ssl.SSLContext) -> tuple[list[int], int]:
    with control(context) as stream:
        authenticate(stream)
        stream.sendall(b"PASV\r\n")
        address, port = passive_endpoint(reply(stream, 227))
        raw_data = socket.create_connection((address, port), 2)
        try:
            data = context.wrap_socket(raw_data, server_hostname=address)
        except BaseException:
            raw_data.close()
            raise
        with data:
            data.settimeout(3)
            if data.version() != "TLSv1.3":
                raise RuntimeError("data TLS version mismatch")
            stream.sendall(f"STOR {CLIENT_PATH}\r\n".encode("ascii"))
            reply(stream, 150, "Opening protected data connection")
            data.sendall(PAYLOAD)
            data.unwrap().close()
        reply(stream, 226, "Transfer complete")
        command(stream, "QUIT", 221, "Goodbye")
    return [220, 331, 230, 200, 200, 227, 150, 226, 221], port


def staged_evidence() -> dict[str, object]:
    entries = tuple(sorted(STAGING.iterdir()))
    receipts = tuple(path for path in entries if path.suffix == ".receipt")
    sources = tuple(path for path in entries if path.suffix == ".source")
    if len(entries) != 2 or len(receipts) != 1 or len(sources) != 1:
        raise RuntimeError("staging surface did not contain one exact complete artifact")
    receipt, source = receipts[0], sources[0]
    if receipt.stem != source.stem or UUID(receipt.stem).version != 4:
        raise RuntimeError("staging identity mismatch")
    for path in entries:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 10001
            or metadata.st_gid != 10001
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RuntimeError("staging file confinement mismatch")
    document = json.loads(receipt.read_bytes())
    expected_keys = {
        "archive_sha256",
        "archive_size_bytes",
        "client_path",
        "consumed",
        "created_at_unix_ms",
        "expires_at_unix_ms",
        "printer_uuid",
        "staging_id",
        "version",
    }
    digest = "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()
    if (
        type(document) is not dict
        or set(document) != expected_keys
        or document["version"] != "2"
        or document["staging_id"] != receipt.stem
        or document["printer_uuid"] != PRINTER_UUID
        or document["client_path"] != CLIENT_PATH
        or document["archive_size_bytes"] != len(PAYLOAD)
        or document["archive_sha256"] != digest
        or document["consumed"] is not False
        or source.stat().st_size != len(PAYLOAD)
        or hashlib.sha256(source.read_bytes()).hexdigest() != digest.removeprefix("sha256:")
    ):
        raise RuntimeError("staging receipt did not authorize the exact retained bytes")
    return {"byte_count": len(PAYLOAD), "sha256": digest, "staging_id": receipt.stem}


def mqtt_absent() -> bool:
    try:
        socket.create_connection((HOST, 8883), 0.5).close()
    except OSError:
        return True
    raise RuntimeError("MQTT listener unexpectedly reachable")


statuses = readiness()
context = tls_context()
cleanup_chain = cleanup(context)
upload_chain, passive_port = upload(context)
evidence = {
    "certificate_ip_san": HOST,
    "cleanup_replies": cleanup_chain,
    "control_tls": "TLSv1.3",
    "confinement": confinement(),
    "data_tls": "TLSv1.3",
    "mqtt_absent": mqtt_absent(),
    "observed_status_count": len(statuses),
    "passive_address": HOST,
    "passive_port": passive_port,
    "readiness": [503, 200],
    "staged": staged_evidence(),
    "upload_replies": upload_chain,
}
print(json.dumps(evidence, sort_keys=True))
