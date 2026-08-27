#!/usr/bin/env python3
"""Factory producer: prove Console truth on the surface a viewer actually reads.

`provider_console_lifecycle` drives a real Console turn and asserts on the
machine: archived events and the local turn-claim file. `product_console_lifecycle`
asserts on the served state contract but writes `state: completed` itself, with
no provider process. Between them sits the gap the 2026-08-23 wedge fell through:
the archive held the reply, the claim read `terminal`, and the served surface
rendered "Working" for ten hours while every machine-side signal stayed green.

This producer closes it -- a real provider turn, judged by what the browser and
iOS are served:

  live delivery  frames reach the workspace SSE stream during the turn
  settlement     once the reply is served, the state axis stops saying work

The predicates come from `console_served_state_core`, which the shipped
`scripts/qa/console-served-state-e2e.py` also uses, so the factory and the
hand-run harness cannot drift into asserting different things.

Settlement is judged on the structured contract -- run identity, lifecycle,
activity, presentation key, working_set -- never on `display_phase`. Six live
fault-injected drops served "Using shell" and "Thinking"; not one served
"Working", so a label allowlist would have passed the incident it was written
for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path

from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _isolated_provider_home
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.resume_assurance import ProducerRegistration

SCENARIO_ID = "console_served_state"
ASSERTION_LIVE = "live_frames_reach_the_viewer_during_the_turn"
ASSERTION_SETTLED = "served_state_settles_once_the_reply_is_served"

REGISTRATION = ProducerRegistration(
    producer_id="longhouse.console_served_state.v1",
    producer_revision=2,
    scenario_id=SCENARIO_ID,
    scenario_revision=2,
    assertion_cells=((ASSERTION_LIVE, None), (ASSERTION_SETTLED, None)),
    # The subject under test is Longhouse, not a provider release, so this
    # declares no provider subject. Stock Codex is an exact auxiliary vehicle:
    # the factory pins and mounts its release while the proof remains about the
    # Longhouse product.
    providers=(),
    vehicle_provider="codex",
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    # A real provider runs and a real Runtime Host serves the result. Nothing
    # here is reconstructed from fixtures; that is the whole point.
    evidence_classes=("live_token",),
    observed_activity=(
        "live_frame_after_dispatch",
        "reply_served_to_the_viewer",
        "state_axis_settled",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("codex_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "provider_auth_receipt",
        "machine_shipper_receipt",
        "vehicle_dispatch_receipt",
        "console_served_state_observation",
        "cleanup_receipt",
    ),
    required_cleanup=("no_orphan_provider_processes", "canary_session_hidden"),
    implementation="server/zerg/qa/console_served_state.py",
    oracle_source="server/zerg/qa/console_served_state_core.py",
    oracle_entrypoint="settlement_state",
    executable_module="zerg.qa.console_served_state",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _manifest(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(content),
                "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }
        )
    return entries


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _process_dead(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _process_group_dead(pgid: object) -> bool:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_vehicle_dead(claim: dict[str, object] | None, *, timeout: float = 10.0) -> bool:
    if not isinstance(claim, dict):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id")):
            return True
        time.sleep(0.1)
    return _process_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id"))


def _vehicle_dispatch_receipt(
    claim: dict[str, object],
    *,
    provider_bin: Path,
    model: str,
    session_id: str,
    run_id: str,
) -> dict[str, object]:
    result = claim.get("result") if isinstance(claim.get("result"), dict) else {}
    argv = result.get("argv") if isinstance(result, dict) else None
    binary_bound = False
    model_bound = False
    if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
        try:
            binary_bound = Path(argv[0]).resolve(strict=True) == provider_bin.resolve(strict=True)
        except OSError:
            binary_bound = False
        encoded_model = model.replace("\\", "\\\\").replace('"', '\\"')
        model_bound = f'model="{encoded_model}"' in argv
    identity_bound = (
        claim.get("session_id") == session_id
        and claim.get("run_id") == run_id
        and claim.get("state") == "terminal"
        and isinstance(result, dict)
        and result.get("terminal_state") == "run_completed"
    )
    return {
        "status": "pass" if binary_bound and model_bound and identity_bound else "fail",
        "provider": "codex",
        "session_id": session_id,
        "run_id": run_id,
        "provider_binary_sha256": _sha256_file(provider_bin),
        "qualification_model": model,
        "binary_bound": binary_bound,
        "model_bound": model_bound,
        "identity_bound": identity_bound,
        "claim_state": claim.get("state"),
        "terminal_state": result.get("terminal_state") if isinstance(result, dict) else None,
        "pid": claim.get("pid"),
        "process_group_id": claim.get("process_group_id"),
    }


def assertions_from_report(report: dict) -> dict[str, bool]:
    """Map one harness report onto this producer's assertion cells.

    Kept separate from the run so the mapping is testable without a provider,
    and so a report shape change fails here rather than silently reporting pass.
    """
    live = report.get("first_live_frame_s") is not None and int(report.get("frame_count") or 0) > 0
    # Settlement is only meaningful once the reply is actually served: a turn
    # that never produced anything has nothing to settle from.
    reply_served = bool(report.get("marker_served"))
    settled = reply_served and report.get("settle_latency_s") is not None
    return {ASSERTION_LIVE: live, ASSERTION_SETTLED: settled}


def run_console_served_state(
    root: Path,
    *,
    provider: str,
    device_id: str,
    cwd: str,
    model: str | None = None,
    on_session_created=None,
) -> dict[str, object]:
    from zerg.qa import console_served_state_core as core

    arguments = argparse.Namespace(
        provider=provider,
        device_id=device_id,
        cwd=cwd,
        api_url=None,
        turn_timeout=180.0,
        settle_budget=30.0,
        watch_session=None,
        drop_terminal=False,
        model=model,
    )
    report = core.run(arguments, on_session_created=on_session_created)
    assertions = assertions_from_report(report)
    _write_json(root / "console-served-state-observation.json", report)
    return {
        "schema_version": 1,
        "artifact_kind": "longhouse_console_served_state_result",
        "producer": REGISTRATION.to_dict(),
        # The subject is Longhouse, so the identity carries no provider -- the
        # factory compares this against a contract that pins `provider: null`
        # and rejects the result outright when it names one. The vehicle that
        # actually drove the turn is in the observation below, which is where
        # evidence belongs; it is not part of what this result claims to be.
        "provider": None,
        "vehicle_provider": provider,
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": _now(),
        "status": "pass" if all(assertions.values()) else "fail",
        "observation": report,
        "assertions": assertions,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", action="store_true")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--provider-bin", type=Path)
    parser.add_argument("--provider-version")
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--model")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.registration:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.evidence_root is None:
        print(json.dumps({"status": "fail", "failure_code": "evidence_root_missing"}))
        return 2
    if any(value in (None, "") for value in (args.engine, args.provider_bin, args.provider_version, args.repo_root, args.model)):
        print(json.dumps({"status": "fail", "failure_code": "vehicle_arguments_missing"}))
        return 2

    from zerg.qa.console_served_state_core import Client

    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    cleanup: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "longhouse_console_served_state_cleanup_receipt",
        "status": "fail",
        "orphan_count": 0,
        "requirements": {
            "no_orphan_provider_processes": False,
            "canary_session_hidden": False,
        },
    }
    provider = "codex"
    device_id: str | None = None
    session_id: str | None = None
    shipper: TranscriptShipper | None = None
    vehicle_claim: dict[str, object] | None = None
    result: dict[str, object] | None = None
    failure: Exception | None = None
    api_url = str(os.environ.get(RUNTIME_API_URL_ENV) or "").strip().rstrip("/")
    token = str(os.environ.get(RUNTIME_AGENTS_TOKEN_ENV) or "").strip()
    environment: dict[str, str] = {}
    try:
        if not api_url or not token:
            raise RuntimeError(f"{RUNTIME_API_URL_ENV} and {RUNTIME_AGENTS_TOKEN_ENV} are required")
        home = _isolated_provider_home()
        workspace = home / "c" / "w"
        engine_evidence = home / "c" / "e"
        longhouse_home = home / "c" / "lh"
        workspace.mkdir(mode=0o700, parents=True)
        engine_evidence.mkdir(mode=0o700, parents=True)
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "LONGHOUSE_CODEX_BIN": str(args.provider_bin),
                "LONGHOUSE_ENGINE_BIN": str(args.engine),
                "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
                "LONGHOUSE_LAUNCH_ACTOR": "automation",
                "LONGHOUSE_LAUNCH_SURFACE": "product-e2e",
                "CODEX_MODEL": str(args.model),
            }
        )
        version = subprocess.run(
            [str(args.provider_bin), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        expected_version = f"codex-cli {args.provider_version}"
        if version.returncode != 0 or version.stdout.strip() != expected_version:
            raise RuntimeError("staged Codex vehicle version does not match the retained plan")
        provider_receipt = {
            "provider": provider,
            "path": str(args.provider_bin),
            "sha256": _sha256_file(args.provider_bin),
            "version": str(args.provider_version),
            "raw_version_output": version.stdout.strip(),
        }
        _write_json(root / "provider-binary-receipt.json", provider_receipt)
        auth_receipt = login_with_api_key(
            args.provider_bin,
            api_key=str(environment.get("CODEX_API_KEY") or ""),
            environment=environment,
            cwd=workspace,
        )
        environment.pop("CODEX_API_KEY", None)
        environment.pop("OPENAI_API_KEY", None)
        _write_json(root / "provider-auth-receipt.json", auth_receipt)
        args.api_url = api_url
        args.agents_token = token
        shipper = _start_transcript_shipper(
            provider,
            args,
            home=home,
            environment=environment,
            evidence_root=engine_evidence,
            longhouse_home=longhouse_home,
        )
        device_id = str(shipper.receipt["machine_name"])

        def remember_session(created_session_id: str) -> None:
            nonlocal session_id
            session_id = created_session_id

        result = run_console_served_state(
            root,
            provider=provider,
            device_id=device_id,
            cwd=str(workspace),
            model=str(args.model),
            on_session_created=remember_session,
        )
        report = result.get("observation") if isinstance(result.get("observation"), dict) else {}
        run_id = str(report.get("run_id") or "")
        if not run_id:
            raise RuntimeError("served-state oracle returned no vehicle run identity")
        claim_path = Path(environment["LONGHOUSE_HOME"]) / "agent" / "turn-claims" / f"{run_id}.json"
        vehicle_claim = json.loads(claim_path.read_text(encoding="utf-8"))
        if not isinstance(vehicle_claim, dict):
            raise RuntimeError("Console vehicle claim is not an object")
        dispatch = _vehicle_dispatch_receipt(
            vehicle_claim,
            provider_bin=args.provider_bin,
            model=str(args.model),
            session_id=str(session_id or ""),
            run_id=run_id,
        )
        _write_json(root / "vehicle-dispatch-receipt.json", dispatch)
        if dispatch["status"] != "pass":
            raise RuntimeError("Console vehicle did not bind the exact staged binary, model, and run")
        result["provider_binary"] = provider_receipt
        result["vehicle_qualification_model"] = str(args.model)
        result["observation"] = {
            **report,
            "vehicle_binary_bound": True,
            "vehicle_model_bound": True,
        }
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = exc
    finally:
        if vehicle_claim is None and environment.get("LONGHOUSE_HOME"):
            claim_root = Path(environment["LONGHOUSE_HOME"]) / "agent" / "turn-claims"
            for claim_path in sorted(claim_root.glob("*.json")):
                try:
                    candidate = json.loads(claim_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(candidate, dict):
                    vehicle_claim = candidate
        if shipper is not None:
            try:
                shipper_receipt = shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup failure must dominate the result
                failure = failure or exc
                shipper_receipt = {"stopped": False, "process_dead": False, "process_group_dead": False}
            _write_json(root / "machine-shipper-receipt.json", shipper_receipt)
        else:
            shipper_receipt = {"stopped": True, "process_dead": True, "process_group_dead": True}
        hidden = False
        if session_id:
            try:
                hidden_result = Client(api_url, token).request(
                    "PATCH",
                    f"/api/agents/sessions/{session_id}/timeline-visibility",
                    {"hidden": True},
                )
                hidden = hidden_result.get("hidden") is True
            except Exception as exc:  # noqa: BLE001 - cleanup failure is retained below
                failure = failure or exc
        provider_dead = _wait_vehicle_dead(vehicle_claim) if vehicle_claim is not None else True
        machine_dead = all(shipper_receipt.get(field) is True for field in ("stopped", "process_dead", "process_group_dead"))
        cleanup.update(
            {
                "status": "pass" if provider_dead and machine_dead and hidden else "fail",
                "orphan_count": 0 if provider_dead and machine_dead else 1,
                "session_id": session_id,
                "requirements": {
                    "no_orphan_provider_processes": provider_dead and machine_dead,
                    "canary_session_hidden": hidden,
                },
            }
        )
        if cleanup["status"] != "pass" and failure is None:
            failure = RuntimeError("Console vehicle cleanup did not satisfy its required contract")
        if failure is not None:
            cleanup["error_type"] = type(failure).__name__
        _write_json(root / "cleanup-receipt.json", cleanup)

    if failure is not None or result is None:
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_console_served_state_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "vehicle_provider": provider,
            "vehicle_device_id": device_id,
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "console_served_state_failed",
            "observation": {
                "failure_code": "console_served_state_failed",
                "vehicle_provider": provider,
                "vehicle_device_id": device_id,
                "session_id": session_id,
            },
            "assertions": {},
            "error": f"{type(failure).__name__}: {failure}",
        }
        result["artifact_manifest"] = _manifest(root)
        _write_json(root / "result.json", result)
        print(json.dumps(result, sort_keys=True, default=str))
        return 1

    result["artifact_manifest"] = _manifest(root)
    _write_json(root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
