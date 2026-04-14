#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class SimulatorDestination:
    identifier: str | None
    name: str
    os_version: str | None = None
    runtime_identifier: str | None = None
    placeholder: bool = False


def _run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"{' '.join(args)} failed: {detail}")
    return result.stdout


def _parse_version(value: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value or ""))


def _runtime_version_from_identifier(runtime_identifier: str | None) -> str | None:
    match = re.search(r"iOS-(\d+(?:-\d+)*)", runtime_identifier or "")
    if not match:
        return None
    return match.group(1).replace("-", ".")


def _family_rank(name: str) -> int:
    if name.startswith("iPhone"):
        return 2
    if name.startswith("iPad"):
        return 1
    return 0


def _destination_sort_key(destination: SimulatorDestination) -> tuple[int, tuple[int, ...]]:
    return (
        _family_rank(destination.name),
        _parse_version(destination.os_version or _runtime_version_from_identifier(destination.runtime_identifier)),
    )


def _render_destination(destination: SimulatorDestination) -> str:
    if destination.identifier:
        return f"platform=iOS Simulator,id={destination.identifier}"
    if destination.os_version:
        return f"platform=iOS Simulator,OS={destination.os_version},name={destination.name}"
    return f"platform=iOS Simulator,name={destination.name}"


def _parse_showdestinations(output: str) -> list[SimulatorDestination]:
    destinations: list[SimulatorDestination] = []
    for segment in re.findall(r"\{([^}]*)\}", output):
        if "platform:iOS Simulator" not in segment:
            continue
        fields = {key.strip(): value.strip() for key, value in re.findall(r"(\w+):([^,}]+)", segment)}
        destinations.append(
            SimulatorDestination(
                identifier=fields.get("id"),
                name=fields.get("name", ""),
                os_version=fields.get("OS"),
                placeholder="placeholder" in str(fields.get("id", "")),
            )
        )
    return destinations


def _load_simctl_devices() -> list[SimulatorDestination]:
    try:
        data = json.loads(_run("xcrun", "simctl", "list", "devices", "available", "-j"))
    except Exception:
        return []

    destinations: list[SimulatorDestination] = []
    for runtime_identifier, devices in data.get("devices", {}).items():
        for device in devices or []:
            if not device.get("isAvailable", True):
                continue
            name = str(device.get("name") or "").strip()
            identifier = str(device.get("udid") or "").strip() or None
            if not name or identifier is None:
                continue
            destinations.append(
                SimulatorDestination(
                    identifier=identifier,
                    name=name,
                    os_version=_runtime_version_from_identifier(str(runtime_identifier)),
                    runtime_identifier=str(runtime_identifier),
                )
            )
    return destinations


def _choose_existing_destination(destinations: list[SimulatorDestination]) -> SimulatorDestination | None:
    concrete = [destination for destination in destinations if not destination.placeholder and destination.identifier]
    if not concrete:
        return None
    return sorted(concrete, key=_destination_sort_key, reverse=True)[0]


def _load_available_runtimes() -> list[dict]:
    try:
        data = json.loads(_run("xcrun", "simctl", "list", "runtimes", "available", "-j"))
    except Exception:
        return []
    runtimes = []
    for runtime in data.get("runtimes", []):
        identifier = str(runtime.get("identifier") or "")
        if "iOS" not in identifier or not runtime.get("isAvailable", False):
            continue
        runtimes.append(runtime)
    return runtimes


def _choose_runtime(runtimes: list[dict]) -> dict | None:
    if not runtimes:
        return None
    return max(
        runtimes,
        key=lambda runtime: _parse_version(str(runtime.get("version") or runtime.get("name") or runtime.get("identifier") or "")),
    )


def _choose_device_type(runtime: dict) -> dict | None:
    supported = list(runtime.get("supportedDeviceTypes") or [])
    if not supported:
        return None
    iphones = [device_type for device_type in supported if _family_rank(str(device_type.get("productFamily") or "")) >= 2]
    if iphones:
        return iphones[0]
    ipads = [device_type for device_type in supported if _family_rank(str(device_type.get("productFamily") or "")) >= 1]
    if ipads:
        return ipads[0]
    return supported[0]


def _create_destination(runtime: dict) -> SimulatorDestination | None:
    device_type = _choose_device_type(runtime)
    runtime_identifier = str(runtime.get("identifier") or "").strip()
    device_type_identifier = str(device_type.get("identifier") or "").strip() if device_type else ""
    device_name = str(device_type.get("name") or "").strip() if device_type else ""
    if not runtime_identifier or not device_type_identifier or not device_name:
        return None

    name = f"Longhouse CI {device_name}"
    identifier = _run("xcrun", "simctl", "create", name, device_type_identifier, runtime_identifier).strip()
    if not identifier:
        return None

    return SimulatorDestination(
        identifier=identifier,
        name=name,
        os_version=str(runtime.get("version") or "").strip() or None,
        runtime_identifier=runtime_identifier,
    )


def _print_failure_diagnostics(showdestinations_output: str, simctl_destinations: list[SimulatorDestination], runtimes: list[dict]) -> None:
    print("No concrete iOS Simulator destination found.", file=sys.stderr)
    concrete_names = ", ".join(sorted(destination.name for destination in simctl_destinations)) or "none"
    runtime_names = ", ".join(
        sorted(str(runtime.get("name") or runtime.get("identifier") or "") for runtime in runtimes)
    ) or "none"
    print(f"simctl available devices: {concrete_names}", file=sys.stderr)
    print(f"simctl available iOS runtimes: {runtime_names}", file=sys.stderr)
    print("xcodebuild -showdestinations output:", file=sys.stderr)
    print(showdestinations_output.strip() or "(empty)", file=sys.stderr)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: select_ios_simulator.py <project> <scheme>", file=sys.stderr)
        return 2

    project, scheme = sys.argv[1], sys.argv[2]
    try:
        showdestinations_output = _run(
            "xcodebuild",
            "-project",
            project,
            "-scheme",
            scheme,
            "-showdestinations",
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    destination = _choose_existing_destination(_parse_showdestinations(showdestinations_output))
    if destination is None:
        simctl_destinations = _load_simctl_devices()
        destination = _choose_existing_destination(simctl_destinations)
    else:
        simctl_destinations = []

    if destination is None:
        runtimes = _load_available_runtimes()
        runtime = _choose_runtime(runtimes)
        if runtime is not None:
            destination = _create_destination(runtime)
        if destination is None:
            _print_failure_diagnostics(showdestinations_output, simctl_destinations, runtimes)
            return 1

    print(_render_destination(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
