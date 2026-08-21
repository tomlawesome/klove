from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Final, cast

PREPARE_WORK: Final = Path("/work")
INPUTS: Final = Path("/inputs")
RUN_STATE: Final = Path("/run-state")
SECRETS: Final = Path("/run/klove-secrets")
CONTRACT_ROOT: Final = Path("/opt/klove-ratos/contract")
CONTRACT_PRINTER_CONFIG: Final = CONTRACT_ROOT / "printer.cfg"
CONTRACT_KLOVE_CONFIG: Final = CONTRACT_ROOT / "klove.toml"
CONTRACT_GCODE: Final = CONTRACT_ROOT / "contract.gcode"
ASSET_NAME: Final = "2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz"
RAW_NAME: Final = ASSET_NAME.removesuffix(".xz")
ASSET_SIZE: Final = 2_125_243_800
ASSET_SHA256: Final = "513465cf6b233d73e5c9ed048568493894d77c7c67e93586609f9b7fbc182153"
RAW_SIZE: Final = 8_449_298_944
RAW_SHA256: Final = "63e95ea4f7a362a1fa9adf210d8f069c671341fb8531a4595f35e2ff6e4a1f51"
BOOT_OFFSET: Final = 8_192 * 512
OVERLAY_SIZE: Final = 16 * 1024 * 1024 * 1024
OVERLAY_NAME: Final = "ratos-rpi32-run.qcow2"
MCOPY: Final = "/usr/bin/mcopy"
QEMU_IMG: Final = "/usr/bin/qemu-img"
XZ: Final = "/usr/bin/xz"

ASSET: Final = PREPARE_WORK / ASSET_NAME
PREPARE_RAW: Final = PREPARE_WORK / RAW_NAME
RUNTIME_RAW: Final = INPUTS / RAW_NAME
RUNTIME_KERNEL: Final = INPUTS / "kernel8.Image"
RUNTIME_DTB: Final = INPUTS / "bcm2710-rpi-3-b.dtb"
EXTRACTED: Final = PREPARE_WORK / "extracted"
OVERLAY: Final = RUN_STATE / OVERLAY_NAME
READINESS_FAILURE: Final = RUN_STATE / "contract-prepare-failure.json"

EXPECTED_SSH_BANNER_PREFIX: Final = "SSH-2.0-OpenSSH_8.4p1 Raspbian-5+deb11u5"
EXPECTED_MOONRAKER_VERSION: Final = "v0.9.1-0-g63578ae"
EXPECTED_API_VERSION: Final = [1, 4, 0]
EXPECTED_API_VERSION_STRING: Final = "1.4.0"
EXPECTED_COMPONENTS: Final = {"authorization", "klippy_connection", "machine"}
EXPECTED_DISTRIBUTION: Final = "Raspbian GNU/Linux 11 (bullseye)"
EXPECTED_RATOS_ID: Final = "ratos"
EXPECTED_RATOS_VERSION: Final = "2.1.0"
EXPECTED_KERNEL: Final = "6.1.21-v8+"
EXPECTED_MODEL: Final = "Raspberry Pi 3 Model B"
EXPECTED_SERVICES: Final = {"klipper", "moonraker", "ratos-configurator"}
EXPECTED_MCU_VERSION: Final = "?-20240727_132503-fv-az659-741"
EXPECTED_CONTRACT_COUNTS: Final = {
    "printer.print.cancel": 1,
    "printer.print.pause": 2,
    "printer.print.resume": 1,
}
HTTP_BODY_LIMIT: Final = 1024 * 1024
HTTP_TIMEOUT_SECONDS: Final = 60
# Klippy restarts are slower than ordinary fixture JSON traffic under TCG.
PRINTER_RESTART_TIMEOUT_SECONDS: Final = 300

BOOT_FILES: Final[dict[str, tuple[int, str]]] = {
    "kernel8.img": (
        8_219_600,
        "a503898ef03e6433f3283e984a37e442081f6bff4ac729cf3a8bffc84c53a726",
    ),
    "bcm2710-rpi-3-b.dtb": (
        32_142,
        "83eefb232fe362a75a8cced8d3f5f17af4398e1d121d486e45eff8937a86b0a1",
    ),
    "config.txt": (
        4_102,
        "f0609e9ee0bd5d9d6d50a9a9e2536cb7be0c91cd9b687058cc0357e995477133",
    ),
    "cmdline.txt": (
        131,
        "1959bd17d773dcac9409c64aa54dae64507fdfb82d1a85616e6fa6a6bd1e23e3",
    ),
}
KERNEL_IMAGE_SIZE: Final = 22_407_680
KERNEL_IMAGE_SHA256: Final = "6cc7aad62b32a1efa774955edc10ea302ae015a58abbea683c23287839b097f8"


def _regular_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"expected a regular file: {path.name}")
    return metadata


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _verify(path: Path, expected_size: int, expected_sha256: str) -> None:
    metadata = _regular_file(path)
    if metadata.st_size != expected_size:
        raise RuntimeError(f"unexpected byte size for {path.name}")
    if _digest(path) != expected_sha256:
        raise RuntimeError(f"unexpected SHA-256 for {path.name}")


def _run(arguments: list[str], *, stdout: BinaryIO | None = None) -> None:
    subprocess.run(arguments, check=True, stdout=stdout)  # noqa: S603


def _replace_verified(
    partial: Path,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
    mode: int,
) -> None:
    _verify(partial, expected_size, expected_sha256)
    partial.chmod(mode)
    partial.replace(destination)


def _write_raw() -> None:
    if PREPARE_RAW.exists():
        _verify(PREPARE_RAW, RAW_SIZE, RAW_SHA256)
        PREPARE_RAW.chmod(0o444)
        return
    partial = PREPARE_RAW.with_suffix(f"{PREPARE_RAW.suffix}.partial")
    if partial.exists():
        raise RuntimeError(f"remove the interrupted partial file first: {partial.name}")
    try:
        with partial.open("xb") as output:
            _run([XZ, "--decompress", "--stdout", str(ASSET)], stdout=output)
        _replace_verified(partial, PREPARE_RAW, RAW_SIZE, RAW_SHA256, 0o444)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _extract_boot_file(name: str, expected_size: int, expected_sha256: str) -> None:
    destination = EXTRACTED / name
    if destination.exists():
        _verify(destination, expected_size, expected_sha256)
        destination.chmod(0o444)
        return
    partial = destination.with_suffix(f"{destination.suffix}.partial")
    if partial.exists():
        raise RuntimeError(f"remove the interrupted partial file first: {partial.name}")
    try:
        _run(
            [
                MCOPY,
                "-n",
                "-i",
                f"{PREPARE_RAW}@@{BOOT_OFFSET}",
                f"::{name}",
                str(partial),
            ]
        )
        _replace_verified(partial, destination, expected_size, expected_sha256, 0o444)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _write_kernel_image() -> None:
    compressed = EXTRACTED / "kernel8.img"
    destination = EXTRACTED / "kernel8.Image"
    if destination.exists():
        _verify(destination, KERNEL_IMAGE_SIZE, KERNEL_IMAGE_SHA256)
        destination.chmod(0o444)
        return
    partial = destination.with_suffix(f"{destination.suffix}.partial")
    if partial.exists():
        raise RuntimeError(f"remove the interrupted partial file first: {partial.name}")
    try:
        with gzip.open(compressed, "rb") as source, partial.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
        _replace_verified(
            partial,
            destination,
            KERNEL_IMAGE_SIZE,
            KERNEL_IMAGE_SHA256,
            0o444,
        )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def prepare() -> None:
    _verify(ASSET, ASSET_SIZE, ASSET_SHA256)
    _run([XZ, "--test", str(ASSET)])
    _write_raw()
    if EXTRACTED.exists():
        if not EXTRACTED.is_dir() or EXTRACTED.is_symlink():
            raise RuntimeError("extracted must be a real directory")
    else:
        EXTRACTED.mkdir(mode=0o700)
    for name, identity in BOOT_FILES.items():
        _extract_boot_file(name, *identity)
    _write_kernel_image()
    evidence = {
        "asset": {"bytes": ASSET_SIZE, "name": ASSET_NAME, "sha256": ASSET_SHA256},
        "boot": {
            name: {"bytes": identity[0], "sha256": identity[1]}
            for name, identity in BOOT_FILES.items()
        },
        "kernel8.Image": {
            "bytes": KERNEL_IMAGE_SIZE,
            "sha256": KERNEL_IMAGE_SHA256,
        },
        "raw": {"bytes": RAW_SIZE, "name": RAW_NAME, "sha256": RAW_SHA256},
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))


def _overlay_info() -> Mapping[str, object]:
    output = subprocess.check_output(  # noqa: S603
        [QEMU_IMG, "info", "--output=json", str(OVERLAY)],
        text=True,
    )
    parsed = json.loads(output)
    if not isinstance(parsed, dict):
        raise RuntimeError("qemu-img returned malformed metadata")
    return cast(Mapping[str, object], parsed)


def _verify_runtime_inputs() -> None:
    _verify(RUNTIME_RAW, RAW_SIZE, RAW_SHA256)
    _verify(RUNTIME_KERNEL, KERNEL_IMAGE_SIZE, KERNEL_IMAGE_SHA256)
    dtb_size, dtb_sha256 = BOOT_FILES["bcm2710-rpi-3-b.dtb"]
    _verify(RUNTIME_DTB, dtb_size, dtb_sha256)


def check_overlay() -> None:
    _verify_runtime_inputs()
    overlay_metadata = _regular_file(OVERLAY)
    if stat.S_IMODE(overlay_metadata.st_mode) != 0o666:
        raise RuntimeError("overlay permissions do not match the private runtime contract")
    _run([QEMU_IMG, "check", str(OVERLAY)])
    info = _overlay_info()
    if info.get("format") != "qcow2" or info.get("virtual-size") != OVERLAY_SIZE:
        raise RuntimeError("overlay format or virtual size is unexpected")
    if info.get("backing-filename") != str(RUNTIME_RAW):
        raise RuntimeError("overlay does not use the exact verified raw backing file")


def overlay() -> None:
    _verify_runtime_inputs()
    if OVERLAY.exists():
        raise RuntimeError("a runtime overlay already exists; each evidence run must start fresh")
    partial = OVERLAY.with_suffix(f"{OVERLAY.suffix}.partial")
    if partial.exists():
        raise RuntimeError(f"remove the interrupted partial file first: {partial.name}")
    try:
        _run(
            [
                QEMU_IMG,
                "create",
                "-f",
                "qcow2",
                "-F",
                "raw",
                "-b",
                str(RUNTIME_RAW),
                str(partial),
            ]
        )
        _run([QEMU_IMG, "resize", str(partial), str(OVERLAY_SIZE)])
        partial.chmod(0o666)
        partial.replace(OVERLAY)
        check_overlay()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _http_request(  # noqa: PLR0913 -- bounded internal HTTP fixture primitive.
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    host_header: str = "127.0.0.1",
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    request_headers = {"Accept": "application/json", "Host": host_header}
    if headers is not None:
        request_headers.update(headers)
    try:
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read(HTTP_BODY_LIMIT + 1)
            status = response.status
        except TimeoutError as error:
            raise RuntimeError(f"{path} timed out") from error
    finally:
        connection.close()
    if len(response_body) > HTTP_BODY_LIMIT:
        raise RuntimeError(f"{path} returned an oversized response")
    return status, response_body


def _parse_json(body: bytes, path: str) -> Mapping[str, object]:
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} returned malformed JSON")
    return cast(Mapping[str, object], parsed)


def _http_json(  # noqa: PLR0913 -- bounded internal HTTP fixture primitive.
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    expected_status: int = 200,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> Mapping[str, object]:
    status, response_body = _http_request(
        18080,
        path,
        method=method,
        body=body,
        headers=headers,
        host_header="ratos.local",
        timeout=timeout,
    )
    if status != expected_status:
        raise RuntimeError(f"{path} returned HTTP {status}")
    return _parse_json(response_body, path)


def _result(response: Mapping[str, object], path: str) -> Mapping[str, object]:
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{path} omitted its result object")
    return cast(Mapping[str, object], result)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Moonraker returned malformed {label}")
    return cast(Mapping[str, object], value)


def _probe_ssh() -> str:
    with socket.create_connection(("127.0.0.1", 12222), timeout=10) as connection:
        banner = connection.recv(256).decode("ascii", "replace").strip()
    if not banner.startswith(EXPECTED_SSH_BANNER_PREFIX):
        raise RuntimeError("the RatOS SSH service identity is unexpected")
    return banner


def _probe_moonraker(api_key: str | None = None) -> dict[str, object]:
    headers = None if api_key is None else _api_headers(api_key)
    server = _result(_http_json("/server/info", headers=headers), "/server/info")
    moonraker_version = server.get("moonraker_version")
    components = server.get("components")
    if moonraker_version != EXPECTED_MOONRAKER_VERSION:
        raise RuntimeError("Moonraker returned an unexpected version")
    if server.get("api_version") != EXPECTED_API_VERSION:
        raise RuntimeError("Moonraker returned an unexpected API version")
    if server.get("api_version_string") != EXPECTED_API_VERSION_STRING:
        raise RuntimeError("Moonraker returned an unexpected API version string")
    if not isinstance(components, list) or not all(isinstance(item, str) for item in components):
        raise RuntimeError("Moonraker returned malformed component identities")
    component_names = cast(list[str], components)
    if not EXPECTED_COMPONENTS.issubset(component_names):
        raise RuntimeError("Moonraker omitted required RatOS service components")
    klippy_connected = server.get("klippy_connected")
    klippy_state = server.get("klippy_state")
    if not isinstance(klippy_connected, bool) or not isinstance(klippy_state, str):
        raise RuntimeError("Moonraker returned malformed Klippy connection state")
    return {
        "api_version": server.get("api_version"),
        "api_version_string": server.get("api_version_string"),
        "components": sorted(component_names),
        "klippy_connected": klippy_connected,
        "klippy_state": klippy_state,
        "moonraker_version": moonraker_version,
    }


def _probe_system() -> dict[str, object]:
    system_result = _result(_http_json("/machine/system_info"), "/machine/system_info")
    system_info = _mapping(system_result.get("system_info"), "system identity")
    distribution = _mapping(system_info.get("distribution"), "distribution identity")
    release_info = _mapping(distribution.get("release_info"), "RatOS release identity")
    cpu_info = _mapping(system_info.get("cpu_info"), "CPU identity")
    service_state = _mapping(system_info.get("service_state"), "service state")
    if distribution.get("name") != EXPECTED_DISTRIBUTION:
        raise RuntimeError("the guest distribution identity is unexpected")
    if distribution.get("kernel_version") != EXPECTED_KERNEL:
        raise RuntimeError("the guest kernel identity is unexpected")
    if release_info.get("id") != EXPECTED_RATOS_ID:
        raise RuntimeError("the RatOS release identifier is unexpected")
    if release_info.get("version_id") != EXPECTED_RATOS_VERSION:
        raise RuntimeError("the RatOS release version is unexpected")
    if cpu_info.get("model") != EXPECTED_MODEL:
        raise RuntimeError("the emulated board identity is unexpected")
    services: dict[str, dict[str, str]] = {}
    for service_name in sorted(EXPECTED_SERVICES):
        service = _mapping(service_state.get(service_name), f"{service_name} service state")
        if service.get("active_state") != "active" or service.get("sub_state") != "running":
            raise RuntimeError(f"the {service_name} service is not active and running")
        services[service_name] = {"active_state": "active", "sub_state": "running"}
    return {
        "distribution": {
            "kernel_version": EXPECTED_KERNEL,
            "name": EXPECTED_DISTRIBUTION,
            "release_info": {
                "id": EXPECTED_RATOS_ID,
                "version_id": EXPECTED_RATOS_VERSION,
            },
        },
        "model": EXPECTED_MODEL,
        "services": services,
    }


def _probe_printer() -> dict[str, object]:
    try:
        printer = _result(_http_json("/printer/info"), "/printer/info")
        return {
            key: printer[key]
            for key in ("hostname", "software_version", "state", "state_message")
            if key in printer and isinstance(printer[key], (bool, int, float, str, type(None)))
        }
    except (ConnectionError, json.JSONDecodeError, OSError, RuntimeError) as error:
        return {"available": False, "reason": str(error)}


def probe() -> None:
    evidence = {
        "moonraker": _probe_moonraker(),
        "printer": _probe_printer(),
        "ssh_banner": _probe_ssh(),
        "system": _probe_system(),
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))


def _contract_source(path: Path) -> bytes:
    metadata = _regular_file(path)
    if metadata.st_size <= 0 or metadata.st_size > HTTP_BODY_LIMIT // 2:
        raise RuntimeError(f"contract input has an invalid size: {path.name}")
    return path.read_bytes()


def _contract_identity(path: Path) -> dict[str, object]:
    metadata = _regular_file(path)
    return {"bytes": metadata.st_size, "sha256": _digest(path)}


def _moonraker_api_key() -> str:
    response = _http_json("/access/api_key")
    api_key = response.get("result")
    if not isinstance(api_key, str) or not 32 <= len(api_key) <= 512:
        raise RuntimeError("Moonraker returned a malformed API key")
    if any(ord(character) < 33 or ord(character) > 126 for character in api_key):
        raise RuntimeError("Moonraker returned a malformed API key")
    return api_key


def _api_headers(api_key: str, *, content_type: str | None = None) -> dict[str, str]:
    headers = {"X-Api-Key": api_key}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def _restart_printer(api_key: str) -> bool:
    try:
        restart = _http_json(
            "/printer/restart",
            method="POST",
            body=b"{}",
            headers=_api_headers(api_key, content_type="application/json"),
            timeout=PRINTER_RESTART_TIMEOUT_SECONDS,
        )
    except RuntimeError as error:
        message = str(error)
        if (
            "/printer/restart returned HTTP 504" in message
            or "/printer/restart timed out" in message
        ):
            return False
        raise
    if restart.get("result") != "ok":
        raise RuntimeError("Moonraker returned an unexpected Klippy restart result")
    return True


def _multipart_upload_body(root: str, filename: str, content: bytes) -> tuple[str, bytes]:
    checksum = hashlib.sha256(content).hexdigest()
    boundary = f"klove-ratos-{checksum[:24]}"
    segments = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="root"\r\n\r\n{root}\r\n'.encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="checksum"\r\n\r\n{checksum}\r\n'
        ).encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="print"\r\n\r\nfalse\r\n'.encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(segments)
    if len(body) > HTTP_BODY_LIMIT:
        raise RuntimeError("contract upload body is oversized")
    return f"multipart/form-data; boundary={boundary}", body


def _upload_contract_file(
    api_key: str,
    *,
    root: str,
    filename: str,
    content: bytes,
    expect_print_status: bool,
) -> None:
    content_type, body = _multipart_upload_body(root, filename, content)
    try:
        response = _http_json(
            "/server/files/upload",
            method="POST",
            body=body,
            headers=_api_headers(api_key, content_type=content_type),
            expected_status=201,
        )
    except RuntimeError as error:
        raise RuntimeError(f"upload of {root}/{filename} failed: {error}") from error
    result = _mapping(response, "upload result")
    expected_fields = {"item", "action"}
    if expect_print_status:
        expected_fields.update(("print_started", "print_queued"))
    if set(result) != expected_fields:
        raise RuntimeError("Moonraker returned malformed upload result")
    item = _mapping(result.get("item"), "uploaded item")
    if set(item) != {"root", "path", "modified", "size", "permissions"}:
        raise RuntimeError("Moonraker returned malformed uploaded item")
    if item.get("root") != root or item.get("path") != filename:
        raise RuntimeError("Moonraker returned a mismatched upload target")
    size = item.get("size")
    modified = item.get("modified")
    if isinstance(size, bool) or not isinstance(size, int) or size != len(content):
        raise RuntimeError("Moonraker returned a mismatched upload size")
    if isinstance(modified, bool) or not isinstance(modified, (int, float)) or modified <= 0:
        raise RuntimeError("Moonraker returned a malformed upload timestamp")
    if item.get("permissions") != "rw":
        raise RuntimeError("Moonraker returned unexpected upload permissions")
    if result.get("action") != "create_file":
        raise RuntimeError("Moonraker returned an unexpected upload action")
    if expect_print_status:
        if result.get("print_started") is not False:
            raise RuntimeError("Moonraker unexpectedly started the uploaded file")
        if result.get("print_queued") is not False:
            raise RuntimeError("Moonraker unexpectedly queued the uploaded file")


def _replace_moonraker_configuration(api_key: str) -> bytes:
    existing = _download_contract_file(api_key, root="config", filename="moonraker.conf")
    replacement = _untrust_moonraker_clients(existing)
    content_type, body = _multipart_upload_body("config", "moonraker.conf", replacement)
    response = _http_json(
        "/server/files/upload",
        method="POST",
        body=body,
        headers=_api_headers(api_key, content_type=content_type),
        expected_status=201,
    )
    result = _mapping(response, "Moonraker configuration upload result")
    # RatOS Moonraker 0.9.1 returns ``create_file`` for the canonical config
    # path even after serving its current bytes from that same path.  The
    # subsequent exact byte read is the authoritative replacement proof.
    if set(result) != {"item", "action"} or result.get("action") not in {
        "create_file",
        "modify_file",
    }:
        raise RuntimeError("Moonraker did not replace its exact configuration file")
    item = _mapping(result.get("item"), "Moonraker configuration upload item")
    if (
        set(item) != {"root", "path", "modified", "size", "permissions"}
        or item.get("root") != "config"
        or item.get("path") != "moonraker.conf"
        or item.get("size") != len(replacement)
        or item.get("permissions") != "rw"
    ):
        raise RuntimeError("Moonraker returned a mismatched configuration upload")
    _verify_remote_contract_file(
        api_key, root="config", filename="moonraker.conf", expected=replacement
    )
    restart = _http_json(
        "/server/restart",
        method="POST",
        body=b"{}",
        headers=_api_headers(api_key, content_type="application/json"),
    )
    if restart.get("result") != "ok":
        raise RuntimeError("Moonraker returned an unexpected service restart result")
    return replacement


def _download_contract_file(api_key: str, *, root: str, filename: str) -> bytes:
    quoted = urllib.parse.quote(filename, safe="")
    status, body = _http_request(
        18080,
        f"/server/files/{root}/{quoted}",
        headers=_api_headers(api_key),
        host_header="ratos.local",
    )
    if status != 200:
        raise RuntimeError("Moonraker could not return the exact uploaded contract file")
    return body


def _verify_remote_contract_file(
    api_key: str, *, root: str, filename: str, expected: bytes
) -> None:
    actual = _download_contract_file(api_key, root=root, filename=filename)
    if not secrets.compare_digest(actual, expected):
        raise RuntimeError("Moonraker returned mismatched contract file bytes")


def _classify_klippy_message(value: str) -> str:
    folded = value.casefold()
    if "config" in folded and ("error" in folded or "parse" in folded):
        return "config-parse"
    if "mcu" in folded and ("socket" in folded or "connect" in folded):
        return "mcu-connect-socket"
    if "mcu" in folded and ("protocol" in folded or "version" in folded):
        return "mcu-protocol"
    if "restart" in folded:
        return "restart-pending"
    return "unknown"


def _closed_state(value: object, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _message_evidence(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {"message": "unknown", "message_sha256": None, "message_bytes": 0}
    encoded = value.encode("utf-8")
    return {
        "message": _classify_klippy_message(value),
        "message_sha256": _digest_bytes(encoded),
        "message_bytes": len(encoded),
    }


def _socket_evidence() -> dict[str, object]:
    path = Path("/tmp/klipper_host_mcu")  # noqa: S108 - exact fixture endpoint
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    return {
        "kind": "socket" if stat.S_ISSOCK(metadata.st_mode) else "other",
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _write_readiness_failure(  # noqa: PLR0913 - fixed retained evidence schema
    *,
    stage: str,
    elapsed_seconds: float,
    cycle: dict[str, bool],
    config: bytes,
    server: dict[str, object],
    printer: dict[str, object],
) -> None:
    # Deliberately retain no response body or raw Klippy message.
    evidence = {
        "stage": stage,
        "elapsed_bucket_seconds": int(elapsed_seconds // 30) * 30,
        "connection_cycle": cycle,
        "config_sha256": _digest_bytes(config),
        "server": server,
        "printer": printer,
        "host_mcu_socket": _socket_evidence(),
    }
    partial = READINESS_FAILURE.with_suffix(".partial")
    partial.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    partial.chmod(0o600)
    partial.replace(READINESS_FAILURE)


def _wait_printer_ready(
    timeout_seconds: float,
    api_key: str | None = None,
    *,
    failure_stage: str | None = None,
    config: bytes | None = None,
    restart_acknowledged: bool = False,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    headers = None if api_key is None else _api_headers(api_key)
    cycle = {
        "restart_acknowledged": restart_acknowledged,
        "disconnect_observed": False,
        "reconnect_observed": False,
    }
    start = time.monotonic()
    last_server: dict[str, object] = {"status": "unavailable", "klippy_state": "unknown"}
    last_printer: dict[str, object] = {
        "status": "unavailable",
        "message": "unknown",
        "message_sha256": None,
        "message_bytes": 0,
    }
    while time.monotonic() < deadline:
        try:
            server = _result(_http_json("/server/info", headers=headers), "/server/info")
            last_server = {
                "status": "ok",
                "klippy_state": _closed_state(
                    server.get("klippy_state"), {"startup", "ready", "error", "shutdown"}
                ),
                "klippy_connected": server.get("klippy_connected") is True,
            }
            if server.get("klippy_connected") is False:
                cycle["disconnect_observed"] = True
            if cycle["disconnect_observed"] and server.get("klippy_connected") is True:
                cycle["reconnect_observed"] = True
        except (ConnectionError, json.JSONDecodeError, OSError, RuntimeError):
            server = {}
        try:
            status, body = _http_request(
                18080, "/printer/info", headers=headers, host_header="ratos.local"
            )
            last_printer = {
                "status": "ok" if status == 200 else "error",
                "http_status": status if 200 <= status <= 599 else None,
                **_message_evidence(None),
            }
            try:
                parsed = _parse_json(body, "/printer/info")
                if status == 200:
                    printer = _result(parsed, "/printer/info")
                    last_printer["state"] = _closed_state(
                        printer.get("state"), {"ready", "startup", "error", "shutdown"}
                    )
                    last_printer.update(_message_evidence(printer.get("state_message")))
                else:
                    error = _mapping(_mapping(parsed, "printer error").get("error"), "error")
                    last_printer.update(_message_evidence(error.get("message")))
            except (RuntimeError, json.JSONDecodeError):
                printer = {}
            if (
                status == 200
                and server.get("klippy_connected") is True
                and server.get("klippy_state") == "ready"
                and printer.get("state") == "ready"
            ):
                return
        except (ConnectionError, OSError):
            last_printer = {"status": "unavailable", "http_status": None, **_message_evidence(None)}
        time.sleep(2)
    if failure_stage is not None and config is not None:
        _write_readiness_failure(
            stage=failure_stage,
            elapsed_seconds=time.monotonic() - start,
            cycle=cycle,
            config=config,
            server=last_server,
            printer=last_printer,
        )
    raise RuntimeError("RatOS Klippy did not become ready within the fixed deadline")


def _untrust_moonraker_clients(value: bytes) -> bytes:
    """Replace only the configured authorization allowlist with TEST-NET-1."""
    try:
        text = value.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise RuntimeError("Moonraker configuration is not UTF-8") from error
    if "\r" in text:
        raise RuntimeError("Moonraker configuration uses unsupported line endings")
    lines = text.splitlines(keepends=True)
    authorization = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\n").strip().casefold() == "[authorization]"
    ]
    if len(authorization) != 1:
        raise RuntimeError("Moonraker configuration must contain one authorization section")
    start = authorization[0] + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].lstrip() == lines[index]
            and lines[index].rstrip("\n").startswith("[")
            and lines[index].rstrip("\n").endswith("]")
        ),
        len(lines),
    )
    trusted = [
        index
        for index in range(start, end)
        if lines[index].casefold().startswith("trusted_clients:")
    ]
    if len(trusted) != 1:
        raise RuntimeError("Moonraker configuration must contain one trusted-client setting")
    option = trusted[0]
    continuation_end = option + 1
    while continuation_end < end and lines[continuation_end][:1] in {" ", "\t"}:
        continuation_end += 1
    return b"".join(
        [
            *[line.encode("utf-8") for line in lines[:option]],
            b"trusted_clients:\n  192.0.2.0/24\n",
            *[line.encode("utf-8") for line in lines[continuation_end:]],
        ]
    )


def _contract_status(api_key: str, *, expected_phase: str) -> dict[str, object]:
    path = "/printer/objects/query?configfile&mcu&pause_resume&print_stats&virtual_sdcard"
    result = _result(_http_json(path, headers=_api_headers(api_key)), path)
    status = _mapping(result.get("status"), "contract object status")
    configfile = _mapping(status.get("configfile"), "configfile status")
    settings = _mapping(configfile.get("settings"), "configfile settings")
    required_sections = {"idle_timeout", "mcu", "pause_resume", "printer", "virtual_sdcard"}
    if not required_sections.issubset(settings):
        raise RuntimeError("Klippy omitted required controlled configuration sections")
    mcu_settings = _mapping(settings.get("mcu"), "MCU settings")
    printer_settings = _mapping(settings.get("printer"), "printer settings")
    if mcu_settings.get("serial") != "/tmp/klipper_host_mcu":  # noqa: S108
        raise RuntimeError("Klippy loaded an unexpected MCU route")
    if (
        printer_settings.get("kinematics") != "none"
        or printer_settings.get("max_velocity") != 1.0
        or printer_settings.get("max_accel") != 1.0
    ):
        raise RuntimeError("Klippy loaded unexpected controlled printer limits")

    mcu = _mapping(status.get("mcu"), "MCU status")
    pause_resume = _mapping(status.get("pause_resume"), "pause-resume status")
    print_stats = _mapping(status.get("print_stats"), "print status")
    virtual_sdcard = _mapping(status.get("virtual_sdcard"), "virtual SD status")
    if mcu.get("mcu_version") != EXPECTED_MCU_VERSION:
        raise RuntimeError("RatOS returned an unexpected host MCU identity")
    if print_stats.get("state") != expected_phase:
        raise RuntimeError("RatOS returned an unexpected contract print phase")
    expected_paused = expected_phase == "paused"
    if pause_resume.get("is_paused") is not expected_paused:
        raise RuntimeError("RatOS returned an inconsistent pause state")
    if expected_phase == "standby" and virtual_sdcard.get("is_active") is not False:
        raise RuntimeError("RatOS unexpectedly reported an active virtual SD job")
    if (
        expected_phase in {"paused", "cancelled"}
        and print_stats.get("filename") != "contract.gcode"
    ):
        raise RuntimeError("RatOS returned the wrong contract job identity")
    return {
        "config_sections": sorted(str(section) for section in settings),
        "filename": print_stats.get("filename"),
        "mcu_version": EXPECTED_MCU_VERSION,
        "phase": expected_phase,
    }


def _history_identity(api_key: str, *, expected_status: str) -> tuple[str, float]:
    result = _result(
        _http_json(
            "/server/history/list?limit=1&start=0&order=desc",
            headers=_api_headers(api_key),
        ),
        "/server/history/list",
    )
    count = result.get("count")
    jobs = result.get("jobs")
    if type(count) is not int or count != 1 or not isinstance(jobs, list) or len(jobs) != 1:
        raise RuntimeError("Moonraker did not return one current history job")
    job = _mapping(jobs[0], "history job")
    job_id = job.get("job_id")
    start_time = job.get("start_time")
    if (
        not isinstance(job_id, str)
        or not 6 <= len(job_id) <= 16
        or any(character not in "0123456789ABCDEF" for character in job_id)
        or isinstance(start_time, bool)
        or not isinstance(start_time, (int, float))
        or start_time < 0
        or job.get("filename") != "contract.gcode"
        or job.get("status") != expected_status
    ):
        raise RuntimeError("Moonraker returned an unexpected immutable history identity")
    return job_id, float(start_time)


def _runner_history_evidence() -> tuple[str, float]:
    raw = sys.stdin.buffer.read(HTTP_BODY_LIMIT + 1)
    if not raw or len(raw) > HTTP_BODY_LIMIT:
        raise RuntimeError("RatOS contract runner history evidence is missing or oversized")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("RatOS contract runner history evidence is malformed") from error
    if not isinstance(document, dict) or set(document) != {"history"}:
        raise RuntimeError("RatOS contract runner history evidence has unexpected fields")
    history = _mapping(document["history"], "contract runner history")
    if set(history) != {"job_id", "phases", "start_time"}:
        raise RuntimeError("RatOS contract runner history identity has unexpected fields")
    phases = _mapping(history["phases"], "contract runner history phases")
    if phases != {
        "started": "in_progress",
        "faulted_pause": "in_progress",
        "cancelled": "cancelled",
    }:
        raise RuntimeError("RatOS contract runner history phases are inconsistent")
    job_id = history["job_id"]
    start_time = history["start_time"]
    if (
        not isinstance(job_id, str)
        or not 6 <= len(job_id) <= 16
        or any(character not in "0123456789ABCDEF" for character in job_id)
        or isinstance(start_time, bool)
        or not isinstance(start_time, (int, float))
        or start_time < 0
    ):
        raise RuntimeError("RatOS contract runner history identity is malformed")
    return job_id, float(start_time)


def _write_private(destination: Path, value: bytes) -> None:
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"refusing to replace existing secret file: {destination.name}")
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.new")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def init_secrets() -> None:
    if os.geteuid() != 10001 or os.getegid() != 10001:
        raise RuntimeError("secret-volume initialization requires exact unprivileged uid/gid")
    metadata = SECRETS.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or SECRETS.is_symlink():
        raise RuntimeError("contract secret mount is not a real directory")
    identity = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
    if identity != (10001, 10001, 0o700):
        raise RuntimeError("contract secret volume has unexpected initial ownership or mode")
    if any(SECRETS.iterdir()):
        raise RuntimeError("contract secret volume must be empty before initialization")
    print(json.dumps({"status": "initialized"}, sort_keys=True))


def _require_secret_directory() -> None:
    metadata = SECRETS.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or SECRETS.is_symlink():
        raise RuntimeError("contract secret mount is not a real directory")
    identity = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
    if os.geteuid() != 10001 or identity != (10001, 10001, 0o700):
        raise RuntimeError("contract secret mount has unexpected ownership or mode")


def _read_secret(name: str) -> str:
    path = SECRETS / name
    metadata = _regular_file(path)
    identity = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
    if identity != (10001, 10001, 0o600):
        raise RuntimeError(f"contract secret file has unexpected identity or mode: {name}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"contract secret file is empty: {name}")
    return value


def contract_prepare() -> None:
    _require_secret_directory()
    if any(SECRETS.iterdir()):
        raise RuntimeError("contract secret volume must be empty before preparation")
    printer_config = _contract_source(CONTRACT_PRINTER_CONFIG)
    klove_config = _contract_source(CONTRACT_KLOVE_CONFIG)
    contract_gcode = _contract_source(CONTRACT_GCODE)
    api_key = _moonraker_api_key()
    # A stock RatOS image has no configured virtual printer, so Klippy cannot
    # be ready until this exact COW-only fixture is installed and restarted.
    _upload_contract_file(
        api_key,
        root="config",
        filename="printer.cfg",
        content=printer_config,
        expect_print_status=False,
    )
    _verify_remote_contract_file(
        api_key, root="config", filename="printer.cfg", expected=printer_config
    )
    printer_restart_acknowledged = _restart_printer(api_key)
    _wait_printer_ready(
        900,
        failure_stage="after_printer_restart",
        config=printer_config,
        restart_acknowledged=printer_restart_acknowledged,
    )
    moonraker_config = _replace_moonraker_configuration(api_key)
    _wait_printer_ready(
        300,
        api_key,
        failure_stage="after_moonraker_auth_restart",
        config=printer_config,
        restart_acknowledged=True,
    )
    invalid_status, _invalid_body = _http_request(
        18080,
        "/printer/objects/query?print_stats",
        headers=_api_headers("0" * 32),
        host_header="ratos.local",
    )
    if invalid_status != 401:
        raise RuntimeError("RatOS did not reject an invalid Moonraker API key")
    _upload_contract_file(
        api_key,
        root="gcodes",
        filename="contract.gcode",
        content=contract_gcode,
        expect_print_status=True,
    )
    _verify_remote_contract_file(
        api_key, root="gcodes", filename="contract.gcode", expected=contract_gcode
    )
    printer_restart_acknowledged = _restart_printer(api_key)
    _wait_printer_ready(
        300,
        api_key,
        failure_stage="after_final_printer_restart",
        config=printer_config,
        restart_acknowledged=printer_restart_acknowledged,
    )
    printer_evidence = _contract_status(api_key, expected_phase="standby")
    _verify_remote_contract_file(
        api_key, root="config", filename="printer.cfg", expected=printer_config
    )
    _verify_remote_contract_file(
        api_key, root="gcodes", filename="contract.gcode", expected=contract_gcode
    )

    _write_private(SECRETS / "moonraker-api-key", f"{api_key}\n".encode())
    _write_private(
        SECRETS / "moonraker-config-sha256", f"{_digest_bytes(moonraker_config)}\n".encode()
    )
    _write_private(SECRETS / "klove-token", f"{secrets.token_hex(32)}\n".encode())
    _write_private(SECRETS / "config.toml", klove_config)
    evidence = {
        "contract_gcode": _contract_identity(CONTRACT_GCODE),
        "klove_config": _contract_identity(CONTRACT_KLOVE_CONFIG),
        "moonraker_config": {"sha256": _digest_bytes(moonraker_config)},
        "printer": printer_evidence,
        "printer_config": _contract_identity(CONTRACT_PRINTER_CONFIG),
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))


def wait_service(service: str) -> None:
    if service == "proxy":
        port, path, expected = 9126, "/health", {"status": "ok"}
    elif service == "klove":
        port, path, expected = 8080, "/health/ready", {"status": "ready"}
    else:
        raise RuntimeError("unknown contract service")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            status, body = _http_request(port, path, timeout=5)
            if status == 200 and _parse_json(body, path) == expected:
                return
        except (ConnectionError, json.JSONDecodeError, OSError, RuntimeError):
            pass
        time.sleep(1)
    raise RuntimeError(f"the {service} contract service did not become ready")


def contract_evidence() -> None:
    api_key = _read_secret("moonraker-api-key")
    runner_history = _runner_history_evidence()
    moonraker_config_sha256 = _read_secret("moonraker-config-sha256")
    if len(moonraker_config_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in moonraker_config_sha256
    ):
        raise RuntimeError("Moonraker configuration fingerprint is malformed")
    printer_config = _contract_source(CONTRACT_PRINTER_CONFIG)
    contract_gcode = _contract_source(CONTRACT_GCODE)
    _verify_remote_contract_file(
        api_key, root="config", filename="printer.cfg", expected=printer_config
    )
    _verify_remote_contract_file(
        api_key, root="gcodes", filename="contract.gcode", expected=contract_gcode
    )
    moonraker_config = _download_contract_file(api_key, root="config", filename="moonraker.conf")
    if _digest_bytes(moonraker_config) != moonraker_config_sha256:
        raise RuntimeError(
            "RatOS Moonraker authorization configuration changed during the contract"
        )
    _contract_status(api_key, expected_phase="cancelled")
    terminal_history = _history_identity(api_key, expected_status="cancelled")
    if terminal_history != runner_history:
        raise RuntimeError(
            "terminal Moonraker history identity differs from faulted-pause evidence"
        )
    status, body = _http_request(9126, "/stats")
    if status != 200:
        raise RuntimeError("the contract proxy did not return dispatch counts")
    proxy_result = _parse_json(body, "/stats")
    counts = _mapping(proxy_result.get("counts"), "contract dispatch counts")
    if counts != EXPECTED_CONTRACT_COUNTS:
        raise RuntimeError("the contract proxy returned unexpected dispatch counts")
    server = _probe_moonraker(api_key)
    evidence = {
        "dispatch_counts": dict(sorted(EXPECTED_CONTRACT_COUNTS.items())),
        "history": {
            "job_id": terminal_history[0],
            "phases": {
                "cancelled": "cancelled",
                "faulted_pause": "in_progress",
                "started": "in_progress",
            },
            "start_time": terminal_history[1],
        },
        "moonraker": {
            "api_version_string": server["api_version_string"],
            "authorization_config_sha256": _digest_bytes(moonraker_config),
            "moonraker_version": server["moonraker_version"],
        },
        "printer": {
            "config_sections": [
                "idle_timeout",
                "mcu",
                "pause_resume",
                "printer",
                "virtual_sdcard",
            ],
            "filename": "contract.gcode",
            "mcu_version": EXPECTED_MCU_VERSION,
            "phase": "cancelled",
        },
        "status": "passed",
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "check-overlay",
            "contract-evidence",
            "contract-prepare",
            "init-secrets",
            "overlay",
            "prepare",
            "probe",
            "wait-service",
        ),
    )
    parser.add_argument("service", nargs="?", choices=("klove", "proxy"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "prepare":
            prepare()
        elif arguments.command == "overlay":
            overlay()
        elif arguments.command == "check-overlay":
            check_overlay()
        elif arguments.command == "probe":
            probe()
        elif arguments.command == "contract-prepare":
            contract_prepare()
        elif arguments.command == "contract-evidence":
            contract_evidence()
        elif arguments.command == "init-secrets":
            init_secrets()
        elif arguments.service is not None:
            wait_service(arguments.service)
        else:
            raise RuntimeError("wait-service requires an exact service name")
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"RatOS emulation {arguments.command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
