#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPO_ROOT / "ios" / "XcodeHarness" / "LonghouseIOS.xcodeproj"
SCHEME = "LonghouseChatStress"
TEST_ID = "LonghouseChatStressUITests/SessionOpenPerformanceUITests/testTimelineTapToTranscriptPaintProfile"
APP_BUNDLE_ID = "ai.longhouse.ios"
PROFILE_CONFIG_PATH = Path("/tmp/longhouse-ios-session-open-profile-config.json")
PROFILE_METRIC_PREFIX = "IOS_PROFILE_METRIC "

NEWEST_DEVICE_PREFERENCES = [
    "iPhone 17 Pro Max",
    "iPhone 17 Pro",
    "iPhone 17",
]
WORST_DEVICE_PREFERENCES = [
    "iPhone 16e",
    "iPhone SE (3rd generation)",
    "iPhone SE (2nd generation)",
    "iPhone 8",
]


@dataclass(frozen=True)
class SimDevice:
    name: str
    udid: str
    runtime: str
    os_version: str


def run(cmd: list[str], *, env: dict[str, str] | None = None, stdout_path: Path | None = None) -> None:
    print("+ " + " ".join(cmd))
    if stdout_path:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("w") as fh:
            proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT, text=True)
    else:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, text=True)
    if proc.returncode != 0:
        detail = f"Command failed with exit {proc.returncode}: {' '.join(cmd)}"
        if stdout_path:
            detail += f"\nLog: {stdout_path}"
        raise RuntimeError(detail)


def output(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout + proc.stderr).strip() or f"{cmd} failed")
    return proc.stdout


def parse_runtime_version(runtime_identifier: str) -> str:
    match = re.search(r"iOS-(\d+(?:-\d+)*)", runtime_identifier)
    return match.group(1).replace("-", ".") if match else "unknown"


def available_iphones() -> list[SimDevice]:
    data = json.loads(output(["xcrun", "simctl", "list", "devices", "available", "-j"]))
    devices: list[SimDevice] = []
    for runtime, entries in data.get("devices", {}).items():
        if "iOS" not in runtime:
            continue
        for entry in entries or []:
            name = str(entry.get("name") or "")
            udid = str(entry.get("udid") or "")
            if not name.startswith("iPhone") or not udid or not entry.get("isAvailable", True):
                continue
            devices.append(SimDevice(name=name, udid=udid, runtime=runtime, os_version=parse_runtime_version(runtime)))
    return devices


def pick_device(devices: list[SimDevice], preferences: list[str], label: str) -> SimDevice:
    by_name = {device.name: device for device in devices}
    for name in preferences:
        if name in by_name:
            return by_name[name]
    if not devices:
        raise RuntimeError("No available iPhone simulators found.")
    available = ", ".join(sorted({device.name for device in devices}))
    preferred = ", ".join(preferences)
    raise RuntimeError(f"No preferred {label} simulator found. Preferred: {preferred}. Available: {available}")


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def prepare_project() -> None:
    run(["python3", "scripts/build/generate_build_identity.py"])
    run(["bash", "scripts/build/stage_ios_build_identity.sh"])
    run(["xcodegen", "--spec", "ios/XcodeHarness/project.yml", "--project-root", "ios/XcodeHarness"])


def run_profile(device: SimDevice, label: str, event_count: int, delayed_tail_ms: int, out_dir: Path) -> list[dict]:
    run_dir = out_dir / f"{label}-{slug(device.name)}-events-{event_count}"
    run_dir.mkdir(parents=True, exist_ok=True)
    profile_path = run_dir / "profile.jsonl"
    log_path = run_dir / "xcodebuild.log"
    result_bundle = run_dir / "LonghouseChatStress.xcresult"
    derived_data = out_dir / "DerivedData"
    shutil.rmtree(result_bundle, ignore_errors=True)
    profile_path.unlink(missing_ok=True)

    if not is_booted(device.udid):
        run(["xcrun", "simctl", "boot", device.udid], stdout_path=run_dir / "simctl-boot.log")
    run(["xcrun", "simctl", "bootstatus", device.udid, "-b"], stdout_path=run_dir / "simctl-bootstatus.log")
    subprocess.run(["xcrun", "simctl", "uninstall", device.udid, APP_BUNDLE_ID], cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    PROFILE_CONFIG_PATH.write_text(
        json.dumps(
            {
                "outputPath": str(profile_path),
                "eventCount": event_count,
                "delayedTailMs": delayed_tail_ms,
            },
            sort_keys=True,
        )
        + "\n"
    )
    cmd = [
        "xcodebuild",
        "-project",
        str(PROJECT.relative_to(REPO_ROOT)),
        "-scheme",
        SCHEME,
        "-destination",
        f"platform=iOS Simulator,id={device.udid}",
        "-derivedDataPath",
        str(derived_data),
        "-resultBundlePath",
        str(result_bundle),
        "-test-timeouts-enabled",
        "YES",
        "-default-test-execution-time-allowance",
        "180",
        "-maximum-test-execution-time-allowance",
        "240",
        "-only-testing:" + TEST_ID,
        "test",
    ]
    try:
        run(cmd, stdout_path=log_path)
    finally:
        PROFILE_CONFIG_PATH.unlink(missing_ok=True)
    records = read_profile_records(profile_path, log_path)
    if not records:
        raise RuntimeError(f"Profile metrics were not written: {profile_path}. See {log_path}")
    for record in records:
        record["deviceName"] = device.name
        record["osVersion"] = device.os_version
        record["eventCount"] = event_count
        record["deviceLabel"] = label
        record["artifactDir"] = str(run_dir)
    profile_path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n")
    return records


def read_profile_records(profile_path: Path, log_path: Path) -> list[dict]:
    if profile_path.exists():
        records = [json.loads(line) for line in profile_path.read_text().splitlines() if line.strip()]
        if records:
            return records

    records: list[dict] = []
    for line in log_path.read_text(errors="replace").splitlines():
        _, found, payload = line.partition(PROFILE_METRIC_PREFIX)
        if not found:
            continue
        records.append(json.loads(payload))
    return records


def is_booted(udid: str) -> bool:
    data = json.loads(output(["xcrun", "simctl", "list", "devices", "available", "-j"]))
    for entries in data.get("devices", {}).values():
        for entry in entries or []:
            if entry.get("udid") == udid:
                return entry.get("state") == "Booted"
    return False


def percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * pct))
    return ordered[index]


def summarize(records: list[dict]) -> list[dict]:
    elapsed_by_key: dict[tuple[str, str, int, str], list[int]] = defaultdict(list)
    render_by_key: dict[tuple[str, str, int, str], list[int]] = defaultdict(list)
    for record in records:
        key = (
            record["deviceLabel"],
            record["deviceName"],
            int(record["eventCount"]),
            record["scenario"],
        )
        elapsed_by_key[key].append(int(record["elapsedMs"]))
        if record.get("renderMs") is not None:
            render_by_key[key].append(int(record["renderMs"]))
    rows = []
    for (device_label, device_name, event_count, scenario), values in sorted(elapsed_by_key.items()):
        render_values = render_by_key[(device_label, device_name, event_count, scenario)]
        rows.append(
            {
                "deviceLabel": device_label,
                "deviceName": device_name,
                "eventCount": event_count,
                "scenario": scenario,
                "samples": len(values),
                "avgMs": round(statistics.mean(values)),
                "p50Ms": percentile(values, 0.50),
                "p90Ms": percentile(values, 0.90),
                "maxMs": max(values),
                "renderP50Ms": percentile(render_values, 0.50) if render_values else "",
            }
        )
    return rows


def write_report(out_dir: Path, devices: dict[str, SimDevice], records: list[dict], rows: list[dict]) -> Path:
    report = out_dir / "report.md"
    lines = [
        "# iOS Session Open Profile",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Devices",
        "",
    ]
    for label, device in devices.items():
        lines.append(f"- {label}: {device.name} ({device.os_version}, {device.udid})")
    lines += [
        "",
        "## Summary",
        "",
        "| Device | Events | Scenario | Samples | Avg ms | P50 ms | P90 ms | Max ms | Render p50 ms |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {deviceName} | {eventCount} | {scenario} | {samples} | {avgMs} | {p50Ms} | {p90Ms} | {maxMs} | {renderP50Ms} |".format(**row)
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "- These are simulator UI-test timings, useful for regression and hang detection, not a substitute for physical-device Instruments traces.",
        "- `*_launch_to_timeline` measures launch until the first timeline row is hittable.",
        "- `*_tap_to_transcript_paint` measures row tap until the WebKit transcript probe reports a rendered transcript with rows and bytes.",
        "- `delayed_tail_*` injects artificial mobile-tail latency before the transcript payload resolves.",
        "",
        "## CI Gate Shape",
        "",
        "- PR smoke: run one event-count profile on the newest available simulator and fail on missing metrics, XCTest crashes, or large regressions.",
        "- Nightly/perf lane: run newest plus worst available simulator, upload `profile.jsonl`, `xcodebuild.log`, and `.xcresult` as artifacts.",
        "- Add explicit threshold flags before making this a blocking CI gate; the current script records and reports but does not enforce timing budgets.",
        "- Physical-device release checks should still use Instruments/Xcode Organizer or MetricKit, because simulator CPU/GPU scheduling is host-dependent.",
        "",
        "## Artifacts",
        "",
        "- `summary.json`: grouped stats",
        "- `records.json`: raw records",
        "- per-run folders: `profile.jsonl`, `xcodebuild.log`, and `LonghouseChatStress.xcresult`",
        "",
    ]
    report.write_text("\n".join(lines) + "\n")
    (out_dir / "records.json").write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    (out_dir / "summary.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Profile Longhouse iOS session-open performance on two simulator extremes.")
    parser.add_argument("--output-dir", default="artifacts/ios-session-profile/latest")
    parser.add_argument("--event-counts", default="120", help="Comma-separated event counts, e.g. 120,500")
    parser.add_argument("--delayed-tail-ms", type=int, default=1500)
    parser.add_argument("--skip-project-gen", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    event_counts = [int(part.strip()) for part in args.event_counts.split(",") if part.strip()]
    out_dir = (REPO_ROOT / args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_project_gen:
        prepare_project()

    devices = available_iphones()
    newest = pick_device(devices, NEWEST_DEVICE_PREFERENCES, "newest")
    worst = pick_device(devices, WORST_DEVICE_PREFERENCES, "worst")
    selected = {"newest": newest, "worst": worst}
    print(f"Selected newest: {newest.name} ({newest.os_version})")
    print(f"Selected worst: {worst.name} ({worst.os_version})")

    records: list[dict] = []
    for label, device in selected.items():
        for event_count in event_counts:
            records.extend(run_profile(device, label, event_count, args.delayed_tail_ms, out_dir))

    rows = summarize(records)
    report = write_report(out_dir, selected, records, rows)
    print(f"\nWrote report: {report}")
    for row in rows:
        print(
            "{deviceName:18} events={eventCount:<4} {scenario:42} "
            "p50={p50Ms:>5}ms p90={p90Ms:>5}ms max={maxMs:>5}ms".format(**row)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
