#!/usr/bin/env python3
"""Hosted provider-live route E2E.

This proves the full Runtime Host -> Machine Agent -> local provider-live
contract for a configured dogfood machine. It does not use generic remote shell:
it calls the typed `/api/agents/machines/{device_id}/provider-live-proof`
surface and verifies both version-match success and typed mismatch rejection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SUPPORTED_PROVIDERS = ("codex", "claude", "opencode", "antigravity")
DEFAULT_USER_AGENT = "sauron-provider-live-proof/1"
DEFAULT_MISMATCH_VERSION = "9.9.9-longhouse-route-e2e"
RETRYABLE_STATUS_CODES = {0, 408, 429, 500, 502, 503, 504}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _longhouse_home() -> Path:
    return Path(os.environ.get("LONGHOUSE_HOME") or Path.home() / ".longhouse").expanduser()


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _machine_state() -> dict[str, Any]:
    path = _longhouse_home() / "machine" / "state.json"
    if not path.exists():
        return {}
    return _load_json(path)


def _default_api_url() -> str | None:
    return os.environ.get("LONGHOUSE_API_URL") or _machine_state().get("runtime_url")


def _default_device_id() -> str | None:
    return os.environ.get("LONGHOUSE_DEVICE_ID") or _machine_state().get("machine_name")


def _default_token_file() -> Path:
    return _longhouse_home() / "machine" / "device-token"


def _default_proof_dir() -> Path:
    return Path(os.environ.get("LONGHOUSE_PROVIDER_LIVE_PROOF_DIR") or _longhouse_home() / "provider-live-proof")


def _read_token(token_file: Path) -> str:
    token = token_file.expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"{token_file} is empty")
    return token


def _read_expected_version(provider: str, proof_dir: Path, overrides: dict[str, str]) -> str:
    if provider in overrides:
        return overrides[provider]
    path = proof_dir / f"{provider}.json"
    if not path.exists():
        raise ValueError(f"missing provider live-proof sidecar for {provider}: {path}")
    payload = _load_json(path)
    version = str(payload.get("provider_version") or "").strip()
    if not version:
        raise ValueError(f"provider live-proof sidecar for {provider} has no provider_version: {path}")
    return version


def _parse_expected(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--expected must be provider=version, got {value!r}")
        provider, version = value.split("=", 1)
        provider = provider.strip()
        version = version.strip()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported provider in --expected: {provider}")
        if not version:
            raise ValueError(f"--expected for {provider} has an empty version")
        parsed[provider] = version
    return parsed


def _auto_providers(proof_dir: Path) -> list[str]:
    providers: list[str] = []
    for provider in SUPPORTED_PROVIDERS:
        path = proof_dir / f"{provider}.json"
        if not path.exists():
            continue
        try:
            payload = _load_json(path)
        except Exception:
            continue
        version = str(payload.get("provider_version") or "").strip()
        if version:
            providers.append(provider)
    return providers


def _selected_providers(raw: list[str] | None, proof_dir: Path) -> list[str]:
    values = raw or ["auto"]
    providers: list[str] = []
    for value in values:
        if value == "all":
            providers.extend(SUPPORTED_PROVIDERS)
        elif value == "auto":
            providers.extend(_auto_providers(proof_dir))
        elif value in SUPPORTED_PROVIDERS:
            providers.append(value)
        else:
            raise ValueError(f"unsupported provider: {value}")
    deduped: list[str] = []
    for provider in providers:
        if provider not in deduped:
            deduped.append(provider)
    if not deduped:
        raise ValueError(f"no provider live-proof sidecars found in {proof_dir}")
    return deduped


def _request_json(
    *,
    method: str,
    url: str,
    token: str,
    user_agent: str,
    body: dict[str, Any] | None = None,
    timeout_s: float,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
        "X-Agents-Token": token,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw_body": raw[-2000:]}
        return exc.code, payload
    except (TimeoutError, urllib.error.URLError) as exc:
        return 0, {"detail": {"code": "request_error", "message": str(exc)}}


def _detail_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    return str(code) if code is not None else None


def _detail_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        message = detail.get("message")
        return str(message) if message is not None else None
    return None


def _is_retryable_response(status: int, payload: dict[str, Any]) -> bool:
    if status in RETRYABLE_STATUS_CODES:
        return True
    if status == 409 and "already in flight" in (_detail_message(payload) or "").lower():
        return True
    return False


def _response_attempt(status: int, payload: dict[str, Any], *, retryable: bool) -> dict[str, Any]:
    attempt: dict[str, Any] = {"status_code": status, "retryable": retryable}
    code = _detail_code(payload)
    if code:
        attempt["code"] = code
    message = _detail_message(payload)
    if message:
        attempt["message"] = message[:240]
    return attempt


def _machine_supports(
    *,
    api_url: str,
    device_id: str,
    token: str,
    user_agent: str,
    timeout_s: float,
) -> dict[str, Any]:
    status, payload = _request_json(
        method="GET",
        url=f"{api_url}/api/agents/machines",
        token=token,
        user_agent=user_agent,
        timeout_s=timeout_s,
    )
    if status != 200:
        raise RuntimeError(f"machines directory returned HTTP {status}: {payload}")
    machines = payload.get("machines")
    if not isinstance(machines, list):
        raise RuntimeError(f"machines directory returned malformed payload: {payload}")
    for machine in machines:
        if isinstance(machine, dict) and machine.get("device_id") == device_id:
            return machine
    raise RuntimeError(f"machine {device_id!r} was not present in machines directory")


def _post_live_proof(
    *,
    api_url: str,
    device_id: str,
    token: str,
    user_agent: str,
    provider: str,
    expected_version: str,
    process_timeout_s: int,
    http_timeout_s: float,
) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "provider": provider,
        "publish": True,
        "timeout_secs": process_timeout_s,
        "expected_provider_version": expected_version,
    }
    return _request_json(
        method="POST",
        url=f"{api_url}/api/agents/machines/{device_id}/provider-live-proof",
        token=token,
        user_agent=user_agent,
        body=body,
        timeout_s=http_timeout_s,
    )


def _post_live_proof_with_retry(
    *,
    args: argparse.Namespace,
    provider: str,
    expected_version: str,
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    max_attempts = max(1, int(args.attempts or 1))
    for attempt_index in range(max_attempts):
        status, payload = _post_live_proof(
            api_url=args.api_url,
            device_id=args.device_id,
            token=args.token,
            user_agent=args.user_agent,
            provider=provider,
            expected_version=expected_version,
            process_timeout_s=args.process_timeout_s,
            http_timeout_s=args.http_timeout_s,
        )
        retryable = _is_retryable_response(status, payload)
        attempts.append(_response_attempt(status, payload, retryable=retryable))
        if not retryable or attempt_index == max_attempts - 1:
            return status, payload, attempts
        if args.retry_delay_s > 0:
            time.sleep(args.retry_delay_s)
    raise AssertionError("unreachable")


def _artifact_version_match(result: dict[str, Any]) -> dict[str, Any]:
    route_result = result.get("result")
    if not isinstance(route_result, dict):
        return {}
    match = route_result.get("provider_version_match")
    return match if isinstance(match, dict) else {}


def _artifact_verdict(result: dict[str, Any]) -> str | None:
    route_result = result.get("result")
    if not isinstance(route_result, dict):
        return None
    artifact = route_result.get("artifact")
    if not isinstance(artifact, dict):
        return None
    value = artifact.get("verdict")
    return str(value) if value is not None else None


def _run_provider(args: argparse.Namespace, provider: str, expected_version: str) -> dict[str, Any]:
    match_status, match_payload, match_attempts = _post_live_proof_with_retry(
        args=args,
        provider=provider,
        expected_version=expected_version,
    )
    result: dict[str, Any] = {
        "provider": provider,
        "expected_provider_version": expected_version,
        "match": {"status_code": match_status, "payload": match_payload},
        "match_attempts": match_attempts,
        "match_attempt_count": len(match_attempts),
    }

    if match_status != 200:
        result.update(
            {
                "status": "fail",
                "failure_code": "provider_live_match_http_error",
                "message": f"{provider} match proof returned HTTP {match_status}",
            }
        )
        return result

    version_match = _artifact_version_match(match_payload)
    if version_match.get("status") != "match":
        result.update(
            {
                "status": "fail",
                "failure_code": "provider_live_version_match_missing",
                "message": f"{provider} match proof did not report version_match=match",
            }
        )
        return result

    verdict = (_artifact_verdict(match_payload) or "").lower()
    if args.require_verdict == "green" and verdict != "green":
        result.update(
            {
                "status": "fail",
                "failure_code": "provider_live_verdict_not_green",
                "message": f"{provider} match proof returned verdict={verdict or '<missing>'}",
            }
        )
        return result
    if args.require_verdict == "non-red" and verdict == "red":
        result.update(
            {
                "status": "fail",
                "failure_code": "provider_live_verdict_red",
                "message": f"{provider} match proof returned red",
            }
        )
        return result

    if not args.skip_mismatch:
        mismatch_status, mismatch_payload, mismatch_attempts = _post_live_proof_with_retry(
            args=args,
            provider=provider,
            expected_version=args.mismatch_version,
        )
        result["mismatch"] = {"status_code": mismatch_status, "payload": mismatch_payload}
        result["mismatch_attempts"] = mismatch_attempts
        result["mismatch_attempt_count"] = len(mismatch_attempts)
        code = _detail_code(mismatch_payload)
        if mismatch_status != 409 or code != "provider_version_mismatch":
            result.update(
                {
                    "status": "fail",
                    "failure_code": "provider_live_mismatch_not_typed",
                    "message": f"{provider} mismatch proof returned HTTP {mismatch_status} code={code}",
                }
            )
            return result

    result.update({"status": "pass", "verdict": verdict or None, "version_match": version_match})
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    args.api_url = (args.api_url or _default_api_url() or "").rstrip("/")
    args.device_id = args.device_id or _default_device_id() or ""
    if not args.api_url:
        raise ValueError("--api-url is required when machine state has no runtime_url")
    if not args.device_id:
        raise ValueError("--device-id is required when machine state has no machine_name")
    args.token_file = (args.token_file or _default_token_file()).expanduser()
    args.proof_dir = (args.proof_dir or _default_proof_dir()).expanduser()
    args.token = _read_token(args.token_file)
    args.providers = _selected_providers(args.provider, args.proof_dir)
    expected_overrides = _parse_expected(args.expected or [])

    machine = _machine_supports(
        api_url=args.api_url,
        device_id=args.device_id,
        token=args.token,
        user_agent=args.user_agent,
        timeout_s=args.http_timeout_s,
    )
    supports = set(machine.get("supports") or [])
    if not machine.get("online"):
        raise RuntimeError(f"machine {args.device_id} is not online")
    missing_support = [provider for provider in args.providers if f"{provider}.live_proof" not in supports]
    if missing_support:
        raise RuntimeError(f"machine {args.device_id} does not advertise live proof for: {', '.join(missing_support)}")

    results = [
        _run_provider(args, provider, _read_expected_version(provider, args.proof_dir, expected_overrides))
        for provider in args.providers
    ]
    failures = [result for result in results if result.get("status") != "pass"]
    return {
        "schema_version": 1,
        "artifact_kind": "provider_live_route_e2e",
        "generated_at": _now_iso(),
        "api_url": args.api_url,
        "device_id": args.device_id,
        "engine_build": machine.get("engine_build"),
        "providers": args.providers,
        "require_verdict": args.require_verdict,
        "mismatch_checked": not args.skip_mismatch,
        "verdict": "red" if failures else "green",
        "failure_count": len(failures),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=None, help="Runtime Host URL; defaults to local machine state.")
    parser.add_argument("--device-id", default=None, help="Machine id; defaults to local machine state.")
    parser.add_argument("--token-file", type=Path, default=None, help="Device token file; defaults to Longhouse home.")
    parser.add_argument("--proof-dir", type=Path, default=None, help="Stable provider live-proof sidecar directory.")
    parser.add_argument(
        "--provider",
        action="append",
        choices=[*SUPPORTED_PROVIDERS, "all", "auto"],
        help=(
            "Provider to prove. Repeat for several. Defaults to auto from live-proof "
            "sidecars. Use all to require every provider."
        ),
    )
    parser.add_argument(
        "--expected",
        action="append",
        default=[],
        help="Expected provider version override as provider=version. Defaults to provider live-proof sidecars.",
    )
    parser.add_argument("--mismatch-version", default=DEFAULT_MISMATCH_VERSION)
    parser.add_argument("--skip-mismatch", action="store_true", help="Skip the typed mismatch rejection check.")
    parser.add_argument(
        "--require-verdict",
        choices=["green", "non-red", "any"],
        default="green",
        help="Artifact verdict requirement for the positive route proof.",
    )
    parser.add_argument("--process-timeout-s", type=int, default=120)
    parser.add_argument("--http-timeout-s", type=float, default=180.0)
    parser.add_argument(
        "--attempts",
        type=int,
        default=2,
        help="Per-leg attempts for transient hosted dispatch failures.",
    )
    parser.add_argument(
        "--retry-delay-s",
        type=float,
        default=2.0,
        help="Delay between transient retry attempts.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--artifact", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run(args)
    except Exception as exc:
        payload = {
            "schema_version": 1,
            "artifact_kind": "provider_live_route_e2e",
            "generated_at": _now_iso(),
            "verdict": "red",
            "failure_count": 1,
            "failure_code": "provider_live_route_e2e_setup_failed",
            "message": f"{type(exc).__name__}: {exc}",
        }
    if args.artifact:
        args.artifact.expanduser().parent.mkdir(parents=True, exist_ok=True)
        args.artifact.expanduser().write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload.get("verdict") == "green":
        print(f"provider-live route E2E green for {', '.join(payload.get('providers') or [])}")
    else:
        print(payload.get("message") or "provider-live route E2E failed", file=sys.stderr)
    return 0 if payload.get("verdict") == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
