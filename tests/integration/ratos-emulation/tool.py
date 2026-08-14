from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Final, cast

PREPARE_WORK: Final = Path("/work")
INPUTS: Final = Path("/inputs")
RUN_STATE: Final = Path("/run-state")
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


def _http_json(path: str) -> Mapping[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", 18080, timeout=10)
    try:
        connection.request(
            "GET", path, headers={"Accept": "application/json", "Host": "ratos.local"}
        )
        response = connection.getresponse()
        body = response.read(1024 * 1024 + 1)
    finally:
        connection.close()
    if response.status != 200:
        raise RuntimeError(f"{path} returned HTTP {response.status}")
    if len(body) > 1024 * 1024:
        raise RuntimeError(f"{path} returned an oversized response")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} returned malformed JSON")
    return cast(Mapping[str, object], parsed)


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


def _probe_moonraker() -> dict[str, object]:
    server = _result(_http_json("/server/info"), "/server/info")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check-overlay", "overlay", "prepare", "probe"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "prepare":
            prepare()
        elif arguments.command == "overlay":
            overlay()
        elif arguments.command == "check-overlay":
            check_overlay()
        else:
            probe()
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"RatOS emulation {arguments.command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
