#!/usr/bin/env python3
"""Compose real Console, web, iOS, and both simlab recovery proofs locally.

Run on the provider-owning Mac: iOS hashes the original native transcript named
by the successful Console run's managed turn claim, never a downloaded copy.
Every requested provider remains in the summary, including upstream failures.
Successful cases still reach the clients when another provider fails. No stage
can be omitted from a passing verdict. This is an operator run, not a CI gate.

Example (start the linked frontend with `make dev` separately):
  make test-terminal-fidelity-gate ARGS="--server-url https://david010.longhouse.ai --browser-url http://127.0.0.1:47200 --device-id cinder --provider codex --cwd /Users/davidrose/git/zerg --ios-destination 'platform=iOS Simulator,id=<simulator-uuid>'"

Quote the destination when invoking Python directly. Credentials come from
--token-env NAME or the canonical local machine/device-token; never pass a token
value. Existing child proof/artifact contracts remain authoritative. The local
summary only links them. Simlab uses its own disposable, synthetic Runtime Host;
its recovery verdicts do not claim recovery against the selected real provider.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ("interrupted-client-recovery", "client-network-recovery")
# Named rather than resolved through `origin`, which is local configuration a
# checkout can point anywhere. What "published" means cannot be a local claim.
CANONICAL_REMOTE = "https://github.com/cipher982/longhouse.git"
REQUIRED_STAGES = ("preflight", "console", "manifest", "web", "ios", "simlab-up", "simlab", "simlab-down")
SIMLAB_STATE = ROOT / "artifacts/simlab/current/simlab.json"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def redact(text: str, token: str = "") -> str:
    if token:
        text = text.replace(token, "[REDACTED]")
    return re.sub(r"\bzdt_[A-Za-z0-9_-]+", "[REDACTED]", text)


def save(path: Path, value: object, token: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(redact(json.dumps(value, indent=2) + "\n", token))
    temporary.replace(path)


def load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError("use an explicit http(s) origin without credentials, query, or path")
    return value.rstrip("/")


def seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return result


def request(url: str, token: str = "") -> dict:
    headers = {"Accept": "application/json", "User-Agent": "longhouse-terminal-fidelity-gate/1"}
    if token:
        headers["X-Agents-Token"] = token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{url}: HTTP {error.code}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{url}: expected JSON object (check linked frontend/authentication)")
    return value


def capture(command: list[str], *, cwd: Path = ROOT, timeout: float = 30) -> str:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"{command[0]} prerequisite failed (exit {completed.returncode}): {completed.stderr[-2000:]}")
    return completed.stdout.strip()


def command_stage(
    name: str, command: list[str], output: Path, environment: dict[str, str], token: str, timeout: float, *, cwd: Path = ROOT
) -> dict:
    """Retain sanitized output on success, failure, timeout, and interruption."""
    log_path = output / f"{name}.log"
    result = {"status": "fail", "log": str(log_path)}
    started = time.monotonic()
    text = ""
    try:
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        ) as process:
            try:
                text, _ = process.communicate(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                result["error"] = "interrupted" if isinstance(error, KeyboardInterrupt) else f"timed out after {timeout}s"
                if isinstance(error, KeyboardInterrupt):
                    result["interrupted"] = True
                for signum in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        pass
                    try:
                        text, _ = process.communicate(timeout=10)
                        break
                    except subprocess.TimeoutExpired as remaining:
                        # A detached descendant may retain stdout after the main
                        # child dies. Keep partial output without waiting forever.
                        text = remaining.output or b""
                        if isinstance(text, bytes):
                            text = text.decode(errors="replace")
                else:
                    if process.stdout:
                        process.stdout.close()
                    process.kill()
                    process.wait(timeout=10)
            result["exit_code"] = process.returncode
            if process.returncode == 0 and "error" not in result:
                result["status"] = "pass"
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = redact(str(error), token)
    log_path.write_text(redact(text, token))
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    save(output / f"{name}.json", result, token)
    return result


def fidelity_case(report: dict, provider: str, device_id: str, claims: Path) -> tuple[dict, dict]:
    """Bind the successful served proof to the exact native source ownership receipt."""
    if report.get("verdict") != "green" or report.get("provider") != provider or report.get("device_id") != device_id:
        raise ValueError("Console proof did not pass for the requested provider and machine")
    session_id = str(UUID(report["session_id"]))
    run_id = str(UUID(report["run_id"]))
    marker = report.get("marker")
    if not isinstance(marker, str) or not marker or any(character.isspace() for character in marker):
        raise ValueError("successful Console proof has no exact final reply marker")
    claim_path = claims / f"{run_id}.json"
    claim = load(claim_path)
    if any(claim.get(key) != expected for key, expected in (("run_id", run_id), ("session_id", session_id), ("provider", provider))):
        raise ValueError("native ownership claim does not match the successful Console run")
    if claim.get("provider_identity_confirmed") is not True or not claim.get("provider_thread_id"):
        raise ValueError("native ownership claim has no confirmed provider identity")
    terminal_state = (claim.get("result") or {}).get("terminal_state")
    if claim.get("state") != "terminal" or terminal_state != "run_completed":
        raise ValueError(f"native ownership claim is not successful: {claim.get('state')}/{terminal_state}")
    source = Path(claim.get("source_path") or "")
    if not source.is_absolute() or not source.is_file():
        raise ValueError("native ownership claim must name the readable original local source file")
    # Resolve neither provider conventions nor guessed rollout filenames. The
    # Machine Agent recorded this path from the actual provider binding.
    evidence = {
        key: claim.get(key)
        for key in ("run_id", "session_id", "provider", "provider_thread_id", "provider_identity_confirmed", "source_path", "state")
    }
    evidence.update(
        terminal_state=terminal_state, claim_path=str(claim_path), claim_sha256=digest(claim_path), source_sha256=digest(source)
    )
    return {"name": f"{device_id}/{provider}", "session_id": session_id, "markers": [marker], "source_path": str(source)}, evidence


def passing(summary: dict) -> bool:
    return (
        all(summary["stages"].get(name, {}).get("status") == "pass" for name in REQUIRED_STAGES)
        and bool(summary["providers"])
        and all(item.get("status") == "pass" for item in summary["providers"])
    )


# What to run next for each boundary. A verdict that names the failed stage but
# not the command that reproduces it sends the reader back through this file.
NEXT_COMMAND = {
    "preflight": "read preflight.json; every entry there is a precondition, not a result",
    "console": "make test-console-served-state-e2e ARGS='--api-url <url> --device-id <id> --provider <name> --cwd <dir>'",
    "manifest": "inspect the console-<provider>-proof.json files; no provider produced a usable case",
    "web": "make test-terminal-fidelity-web FIDELITY_CASES=<cases.json> PLAYWRIGHT_BASE_URL=<url>",
    "ios": "make test-terminal-fidelity-ios FIDELITY_CASES=<cases.json> IOS_DESTINATION=<destination>",
    "simlab-up": "python scripts/qa/simlab.py up --build",
    "simlab": "make simlab-run SCENARIOS='interrupted-client-recovery client-network-recovery'",
    "simlab-down": "python scripts/qa/simlab.py down",
}


def receipt(summary: dict, output: Path) -> dict:
    """Say what was exercised, which boundary failed, and what to run next."""

    provenance = summary["stages"].get("preflight", {}).get("release_provenance") or {}
    # Mirror passing() exactly. A missing or not_run stage is a failed boundary,
    # not a silent pass; treating it as one reported "unknown" for the most
    # common failure there is -- a stage that never got to run.
    failed = next(
        (name for name in REQUIRED_STAGES if summary["stages"].get(name, {}).get("status") != "pass"),
        None,
    )
    if failed is None and any(item.get("status") != "pass" for item in summary["providers"]):
        failed = "console"
    return {
        "product_build": provenance.get("components"),
        "released_build": provenance.get("released"),
        # No current proof carries a provider *version*: the Console report has
        # none and the machine directory reports readiness only. Record what
        # exists under its real name rather than an empty provider_builds that
        # would read as "no providers" instead of "not captured anywhere".
        "provider_readiness": (summary["stages"].get("preflight") or {}).get("machine", {}).get("provider_readiness"),
        "provider_verdicts": {
            item.get("provider"): item.get("verdict") for item in summary["providers"] if isinstance(item, dict)
        },
        "failed_boundary": failed,
        "evidence": str(output / (f"{failed}.json" if failed else "summary.json")),
        "next_command": NEXT_COMMAND.get(failed or "", ""),
    }


def release_provenance(server_build: object) -> dict:
    """What the run is about to exercise: a published release, or a dev build.

    Installed and dogfood builds pass the same stages, so a green run says
    nothing on its own about the binaries a person can actually download.
    `channel` is "release" only for a build cut by scripts/ops/release.sh; a
    dirty tree disqualifies one regardless of channel.
    """

    local = json.loads(capture(["longhouse", "build-identity", "--json"]))
    parts = {"server": server_build, "cli": local.get("facade"), "engine": local.get("engine")}

    def from_a_published_tag(build: object) -> bool:
        # `channel` is self-attested, so is a local tag, and so is `origin`.
        # Only the canonical remote's own refs say what was actually published,
        # so ask it by name, and treat an unanswerable question as "not
        # released" rather than as permission to proceed.
        if not isinstance(build, dict):
            return False
        if build.get("channel") != "release" or build.get("dirty") is not False:
            return False
        version, commit = build.get("version"), build.get("commit")
        if not isinstance(version, str) or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            return False
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            return False
        try:
            listing = capture(
                ["git", "ls-remote", CANONICAL_REMOTE, f"refs/tags/v{version}", f"refs/tags/v{version}^{{}}"],
                cwd=ROOT,
                timeout=60,
            )
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            return False
        published = {line.split("\t")[0] for line in listing.splitlines() if "\t" in line}
        return commit in published

    released = {name: from_a_published_tag(build) for name, build in parts.items()}
    return {
        "released": all(released.values()),
        "components": {
            name: {
                "released": released[name],
                "version": build.get("version") if isinstance(build, dict) else None,
                "commit_short": build.get("commit_short") if isinstance(build, dict) else None,
                "channel": build.get("channel") if isinstance(build, dict) else None,
                "dirty": build.get("dirty") if isinstance(build, dict) else None,
            }
            for name, build in parts.items()
        },
        "engine_path": local.get("engine_path"),
    }


def preflight(args: argparse.Namespace, token: str, home: Path) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("all stages require macOS with Xcode and an installed iOS Simulator; no stages are skipped")
    for tool in ("make", "bun", "uv", "cargo", "xcrun", "xcodebuild", "git"):
        if not shutil.which(tool):
            raise RuntimeError(f"required executable unavailable: {tool}")
    for prerequisite in ("server/.venv/bin/python", "ios/XcodeHarness/LonghouseIOS.xcodeproj/project.pbxproj"):
        if not (ROOT / prerequisite).is_file():
            raise RuntimeError(f"required client/runtime prerequisite unavailable: {prerequisite}; complete repository dev setup")
    match = re.fullmatch(r"platform=iOS Simulator,id=([A-Fa-f0-9-]+)", args.ios_destination)
    if not match:
        raise ValueError("--ios-destination must be platform=iOS Simulator,id=<UUID> (one explicit device for both native runners)")
    udid = str(UUID(match.group(1))).upper()
    devices = json.loads(capture(["xcrun", "simctl", "list", "devices", "available", "--json"]))
    if not any(
        device.get("udid", "").upper() == udid and device.get("isAvailable")
        for group in devices.get("devices", {}).values()
        for device in group
    ):
        raise RuntimeError("selected iOS Simulator is not available; install its runtime in Xcode")
    if not (home / "agent/turn-claims").is_dir() or not args.cwd.is_dir():
        raise RuntimeError("run on the provider-owning Mac with its real agent/turn-claims directory and existing --cwd")
    if SIMLAB_STATE.exists():
        state = load(SIMLAB_STATE)
        for key in ("server_pid", "engine_pid", "proxy_pid"):
            if state.get(key):
                try:
                    os.kill(int(state[key]), 0)
                except ProcessLookupError:
                    continue
                raise RuntimeError("simlab has a live scratch process; finish that run with simlab.py down first")
    server = request(args.server_url + "/api/health")
    directory = request(args.server_url + "/api/agents/machines", token)
    machine = next((item for item in directory.get("machines", []) if item.get("device_id") == args.device_id), None)
    if not machine or not machine.get("online"):
        raise RuntimeError("requested machine is not online on the explicit Runtime Host")
    local_state = load(home / "machine/state.json")
    local_name = local_state.get("machine_name") or platform.node()
    if local_name not in {machine.get("machine_name"), machine.get("device_id")}:
        raise RuntimeError(
            "requested machine does not match this Mac's canonical machine state; native source proof cannot use a remote copy"
        )
    offered = {item.get("provider") for item in machine.get("launch", {}).get("providers", [])}
    missing = set(args.provider) - offered
    if missing:
        raise RuntimeError(f"machine does not advertise requested Console providers: {', '.join(sorted(missing))}")
    browser_directory = request(args.browser_url + "/api/timeline/machines")
    if not any(item.get("device_id") == args.device_id for item in browser_directory.get("machines", [])):
        raise RuntimeError("browser URL must provide authenticated access to the selected machine (use the linked local frontend)")
    browser_health = request(args.browser_url + "/api/health")
    with urllib.request.urlopen(args.browser_url + "/", timeout=20) as response:
        frontend_html = response.read()
    frontend_builds = re.findall(rb"/config\.js\?v=([^\"'\s<>]+)", frontend_html)
    browser_probe = """const { chromium } = require('@playwright/test');
const fs = require('node:fs');
const path = chromium.executablePath();
if (!fs.existsSync(path)) throw new Error('Playwright Chromium unavailable: run bunx playwright install chromium');
const browser = await chromium.launch();
console.log(JSON.stringify({path, version: browser.version()}));
await browser.close();"""
    chromium = json.loads(capture(["bun", "-e", browser_probe], cwd=ROOT / "e2e", timeout=45))
    provenance = release_provenance(server.get("build"))
    if args.require_released_build and not provenance["released"]:
        unreleased = ", ".join(
            f"{name} {value['version']} ({value['channel']}{', dirty' if value['dirty'] else ''})"
            for name, value in sorted(provenance["components"].items())
            if not value["released"]
        )
        raise RuntimeError(
            "--require-released-build was given and these are not published releases: "
            f"{unreleased}. Install the release and re-link before qualifying it; a dogfood "
            "build passing this gate says nothing about what a person can download."
        )
    return {
        "status": "pass",
        "release_provenance": provenance,
        "simulator_udid": udid,
        "platform": platform.platform(),
        "server_build": server.get("build"),
        "browser_runtime_build": browser_health.get("build"),
        "machine": {key: machine.get(key) for key in ("device_id", "machine_name", "engine_build", "provider_readiness")},
        "checkout_sha": capture(["git", "rev-parse", "HEAD"]),
        "tracked_changes_sha256": hashlib.sha256(capture(["git", "diff", "HEAD", "--", "."]).encode()).hexdigest(),
        "source_files": {
            str(path.relative_to(ROOT)): digest(path)
            for path in (
                Path(__file__),
                ROOT / "scripts/qa/console-served-state-e2e.py",
                ROOT / "server/zerg/qa/console_served_state_core.py",
                ROOT / "scripts/qa/terminal-fidelity-ios.py",
                ROOT / "scripts/qa/simlab.py",
                ROOT / "e2e/tests/live/terminal-fidelity.spec.ts",
            )
        },
        "frontend_html_sha256": hashlib.sha256(frontend_html).hexdigest(),
        "frontend_builds": [value.decode(errors="replace") for value in frontend_builds],
        "xcode": capture(["xcodebuild", "-version"]),
        "chromium": chromium,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server-url", type=origin, required=True)
    parser.add_argument(
        "--browser-url", type=origin, required=True, help="linked frontend or explicitly auth-disabled host; no silent browser login"
    )
    parser.add_argument("--device-id", required=True, help="one explicit local Machine Agent, not 'all'")
    parser.add_argument(
        "--provider", action="append", required=True, help="representative Console provider; repeat for multiple, never implicit 'all'"
    )
    parser.add_argument("--cwd", type=Path, required=True, help="existing local provider working directory")
    parser.add_argument("--ios-destination", required=True, help="platform=iOS Simulator,id=<UUID>; quote spaces for direct Python use")
    parser.add_argument(
        "--token-env",
        metavar="NAME",
        help="read Runtime Host token from this environment variable; default: canonical machine/device-token",
    )
    parser.add_argument("--output-dir", type=Path, help="new directory; default artifacts/terminal-fidelity/gate-<unique UTC timestamp>")
    parser.add_argument("--stage-timeout", type=seconds, default=1800, help="finite per-stage ceiling in seconds (default 1800)")
    parser.add_argument("--turn-timeout", type=seconds, default=180)
    parser.add_argument(
        "--require-released-build",
        action="store_true",
        help="refuse to run unless the server, CLI and engine are published releases rather than dev builds",
    )
    args = parser.parse_args()
    if args.device_id == "all" or any(not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name == "all" for name in args.provider):
        parser.error("choose one explicit machine and named representative providers, not 'all'")
    if len(set(args.provider)) != len(args.provider):
        parser.error("each requested provider must occur once")

    def interrupt(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupt)
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output_dir or ROOT / "artifacts/terminal-fidelity" / f"gate-{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    args.cwd = args.cwd.expanduser().resolve()
    home = Path(os.environ.get("LONGHOUSE_HOME") or Path.home() / ".longhouse").expanduser()
    summary = {
        "run_id": stamp,
        "status": "running",
        "server_url": args.server_url,
        "browser_url": args.browser_url,
        "device_id": args.device_id,
        "destination": args.ios_destination,
        "required_stages": list(REQUIRED_STAGES),
        "stages": {},
        "providers": [{"provider": name, "status": "not_run"} for name in args.provider],
    }
    token = ""
    environment = dict(os.environ)
    simlab_started = False

    def persist() -> None:
        save(output / "summary.json", summary, token)

    def stage(name: str, command: list[str], *, cwd: Path = ROOT, timeout: float | None = None) -> dict:
        result = command_stage(name, command, output, environment, token, timeout or args.stage_timeout, cwd=cwd)
        summary["stages"][name] = result
        persist()
        print(f"[fidelity-gate] {name}: {result['status']}", flush=True)
        if result.get("interrupted"):
            raise KeyboardInterrupt
        return result

    persist()
    lock = None
    try:
        token = (os.environ.get(args.token_env, "") if args.token_env else (home / "machine/device-token").read_text()).strip()
        if not token:
            raise RuntimeError("Runtime Host token unavailable; supply --token-env NAME or link this machine first")
        lock_path = ROOT / "artifacts/simlab/.terminal-fidelity.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        summary["stages"]["preflight"] = preflight(args, token, home)
        save(output / "preflight.json", summary["stages"]["preflight"], token)
        persist()
        environment.update(
            LONGHOUSE_RUNTIME_API_URL=args.server_url,
            LONGHOUSE_RUNTIME_AGENTS_TOKEN=token,
            LONGHOUSE_FIDELITY_SERVER_URL=args.server_url,
            LONGHOUSE_FIDELITY_AUTH_TOKEN=token,
            PLAYWRIGHT_BASE_URL=args.browser_url,
            PLAYWRIGHT_API_BASE_URL=args.server_url,
            IOS_DESTINATION=args.ios_destination,
            SIM_UDID=summary["stages"]["preflight"]["simulator_udid"],
        )
        cases, ownership = [], []
        for item in summary["providers"]:
            provider = item["provider"]
            report_path = output / f"console-{provider}-proof.json"
            result = stage(
                f"console-{provider}",
                [
                    sys.executable,
                    str(ROOT / "scripts/qa/console-served-state-e2e.py"),
                    "--api-url",
                    args.server_url,
                    "--device-id",
                    args.device_id,
                    "--provider",
                    provider,
                    "--cwd",
                    str(args.cwd),
                    "--turn-timeout",
                    str(args.turn_timeout),
                ],
            )
            item.update(status="fail", stage=f"console-{provider}", proof=str(report_path))
            try:
                report = load(Path(result["log"]))
                # Child reports may include provider errors. Preserve them, redacting
                # the same token used by the child's existing env-only interface.
                save(report_path, report, token)
                item["verdict"] = report.get("verdict")
                if result["status"] != "pass":
                    raise RuntimeError("Console command failed; inspect retained proof and log")
                case, evidence = fidelity_case(report, provider, args.device_id, home / "agent/turn-claims")
                cases.append(case)
                ownership.append(evidence)
                item.update(status="pass", session_id=case["session_id"])
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                item["error"] = redact(str(error), token)
            persist()
        summary["stages"]["console"] = {"status": "pass" if all(item["status"] == "pass" for item in summary["providers"]) else "fail"}
        manifest = output / "cases.json"
        save(manifest, cases, token)
        save(output / "source-ownership.json", ownership, token)
        summary["stages"]["manifest"] = {
            "status": "pass" if cases else "fail",
            "cases": str(manifest),
            "case_count": len(cases),
            "ownership": str(output / "source-ownership.json"),
        }
        persist()
        if cases:
            # Both clients consume this exact successful subset, never a hand-built
            # marker/session list. The requested provider failures remain above.
            environment.update(LONGHOUSE_FIDELITY_CASES_PATH=str(manifest), PLAYWRIGHT_JSON_OUTPUT_FILE=str(output / "web-proof.json"))
            web = stage(
                "web",
                [
                    "bunx",
                    "playwright",
                    "test",
                    "--config",
                    "playwright.prod.config.js",
                    "tests/live/terminal-fidelity.spec.ts",
                    "--output",
                    str(output / "web"),
                    "--reporter=json",
                ],
                cwd=ROOT / "e2e",
            )
            try:
                proof = load(output / "web-proof.json")
                stats = proof.get("stats", {})
                if (
                    stats.get("expected") != len(cases)
                    or any(stats.get(key, 0) for key in ("unexpected", "flaky", "skipped"))
                    or proof.get("errors")
                ):
                    raise ValueError("web proof must execute every case exactly once with no skipped/flaky/failed tests")
                web["proof"] = str(output / "web-proof.json")
            except (OSError, ValueError) as error:
                web.update(status="fail", error=str(error))
            ios = stage(
                "ios",
                [
                    sys.executable,
                    str(ROOT / "scripts/qa/terminal-fidelity-ios.py"),
                    "--cases",
                    str(manifest),
                    "--output",
                    str(output / "ios-proof.json"),
                ],
            )
            try:
                proof = load(output / "ios-proof.json")
                expected = {case["session_id"]: case for case in cases}
                rows = proof.get("cases", [])
                if proof.get("status") != "pass" or len(rows) != len(cases) or {row.get("session_id") for row in rows} != set(expected):
                    raise ValueError("iOS proof must include every exact manifest case")
                for row in rows:
                    case = expected[row["session_id"]]
                    if (
                        row.get("status") != "pass"
                        or row.get("source_path") != case["source_path"]
                        or row.get("source_sha256_before") != row.get("source_sha256_after")
                        or row.get("source_sha256_after") != digest(Path(case["source_path"]))
                    ):
                        raise ValueError("iOS proof must preserve each original native source file")
                ios["proof"] = str(output / "ios-proof.json")
            except (OSError, ValueError, KeyError) as error:
                ios.update(status="fail", error=str(error))
            # Also protect the original bytes across web viewing, not just iOS.
            for evidence in ownership:
                try:
                    if digest(Path(evidence["source_path"])) != evidence["source_sha256"]:
                        raise ValueError("original native source changed during client verification")
                except (OSError, ValueError) as error:
                    summary["stages"]["manifest"].update(status="fail", error=str(error))
        else:
            for name in ("web", "ios"):
                summary["stages"][name] = {"status": "blocked", "error": "no successful Console case with native ownership evidence"}
        persist()
        # Do not inherit live provider credentials into the disposable recovery lane.
        environment = {
            key: value
            for key, value in environment.items()
            if not re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|AUTH|CREDENTIAL", key, re.IGNORECASE)
        }
        previous_scratch = load(SIMLAB_STATE).get("scratch") if SIMLAB_STATE.exists() else None
        try:
            up = stage("simlab-up", [sys.executable, str(ROOT / "scripts/qa/simlab.py"), "up", "--build"])
        finally:
            # If up refuses an already-running scratch instance, it leaves that
            # state untouched. Do not tear down somebody else's run in cleanup.
            simlab_started = SIMLAB_STATE.exists() and load(SIMLAB_STATE).get("scratch") != previous_scratch
        if up["status"] == "pass":
            recovery = stage("simlab", [sys.executable, str(ROOT / "scripts/qa/simlab.py"), "run", "--deploy", *SCENARIOS])
            try:
                state = load(SIMLAB_STATE)
                proof_path = Path(state["scratch"]) / "artifacts/summary.json"
                proof = load(proof_path)
                rows = proof.get("scenarios", [])
                recovery["proof"] = str(proof_path)
                if (
                    proof.get("status") != "pass"
                    or len(rows) != len(SCENARIOS)
                    or {row.get("scenario") for row in rows} != set(SCENARIOS)
                    or any(row.get("status") != "pass" for row in rows)
                ):
                    raise ValueError("both exact recovery scenarios must pass; incomplete summaries cannot pass")
            except (OSError, ValueError, KeyError) as error:
                recovery.update(status="fail", error=str(error))
    except (Exception, KeyboardInterrupt) as error:
        summary["error"] = redact(f"{type(error).__name__}: {error}", token)
        if "preflight" not in summary["stages"]:
            summary["stages"]["preflight"] = {"status": "fail", "error": summary["error"]}
    finally:
        if simlab_started:
            # simlab owns exact PID/start identities and retains scratch evidence.
            # A failed startup also requires cleanup; never discard its verdicts.
            try:
                stage("simlab-down", [sys.executable, str(ROOT / "scripts/qa/simlab.py"), "down"], timeout=60)
                if SIMLAB_STATE.exists():
                    state = load(SIMLAB_STATE)
                    summary["simlab_artifacts"] = str(Path(state["scratch"]) / "artifacts") if state.get("scratch") else None
            except (Exception, KeyboardInterrupt) as error:
                summary["error"] = redact(f"simlab cleanup failed: {error}", token)
        for name in REQUIRED_STAGES:
            summary["stages"].setdefault(name, {"status": "not_run"})
        for name, result in summary["stages"].items():
            save(output / f"{name}.json", result, token)
        summary["status"] = "pass" if passing(summary) and "error" not in summary else "fail"
        try:
            summary["receipt"] = receipt(summary, output)
        except Exception as error:  # a cleanup helper must never lose the verdict
            summary["receipt"] = {"error": redact(f"{type(error).__name__}: {error}", token)}
        persist()
        if lock:
            lock.close()
    mark = summary["receipt"]
    if summary["status"] != "pass" and mark.get("failed_boundary"):
        print(f"[fidelity-gate] failed at {mark['failed_boundary']}; evidence: {mark['evidence']}", flush=True)
        if mark.get("next_command"):
            print(f"[fidelity-gate] next: {mark['next_command']}", flush=True)
    print(
        f"[fidelity-gate] {summary['status']}"
        f"; released build: {mark.get('released_build')}"
        f"; evidence: {output / 'summary.json'}"
    )
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
