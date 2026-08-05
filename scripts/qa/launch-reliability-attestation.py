#!/usr/bin/env python3
"""Create and verify release-owned launch-reliability dogfood receipts.

The dogfood collector's HMAC challenge is intentionally operator-held and is
therefore diagnostic integrity evidence only. This companion contract uses an
Ed25519 key whose public half is committed to the repository; the private half
is held by the CI/release environment. The signed subject contains the exact
report inputs, source revision, sampled binary hashes, and collection times.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import string
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REPOSITORY = Path(__file__).resolve().parents[2]
REPOSITORY_IDENTITY = "cipher982/longhouse"
TRUSTED_KEYS_PATH = REPOSITORY / "config/qa/launch_reliability_attestation_keys.json"
ARTIFACT_KIND = "launch_reliability_dogfood_attestation"
SCHEMA_VERSION = 1
ISSUER = "longhouse-release-ci"
ALGORITHM = "ed25519"
MAX_RECEIPT_LIFETIME = timedelta(days=90)
MEASURE_NAMES = (
    "automatic_recovery_time",
    "false_red_rate",
    "hidden_failure_rate",
    "unresolved_event_bearing_issue_age",
    "action_coverage",
    "duplicate_replayed_discarded_evidence",
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _validate_subject(subject: dict[str, Any]) -> None:
    if subject.get("artifact_kind") != "launch_reliability_measurements":
        raise ValueError("attestation subject has the wrong report artifact kind")
    if subject.get("report_schema_version") != 2:
        raise ValueError("attestation subject has the wrong report schema version")
    provenance = subject.get("report_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("attestation subject has no report provenance")
    git_sha = provenance.get("git_sha")
    if (
        not isinstance(git_sha, str)
        or len(git_sha) != 40
        or any(character not in string.hexdigits for character in git_sha)
    ):
        raise ValueError("attestation subject has no full source SHA")
    if provenance.get("repository_dirty") is not False or provenance.get("harness_file_dirty") is not False:
        raise ValueError("attestation subject provenance is dirty")
    if provenance.get("repository") != REPOSITORY_IDENTITY:
        raise ValueError("attestation subject has no repository identity")
    report_sha = provenance.get("sha256")
    if (
        not isinstance(report_sha, str)
        or len(report_sha) != 64
        or any(character not in string.hexdigits for character in report_sha)
    ):
        raise ValueError("attestation subject has no report SHA-256")
    inputs = subject.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("attestation subject has no report inputs")
    for name in (
        "matrix_artifacts",
        "provider_harness_artifacts",
        "health_artifacts",
        "dogfood_series_artifacts",
    ):
        values = inputs.get(name)
        if not isinstance(values, list):
            raise ValueError(f"attestation subject input {name} is not a list")
        for value in values:
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"attestation subject input {name} has an invalid SHA-256")
    dogfood = subject.get("dogfood_series")
    if not isinstance(dogfood, dict) or dogfood.get("input_status") != "valid":
        raise ValueError("attestation subject dogfood input is not valid")
    if dogfood.get("episode_count", 0) < 1:
        raise ValueError("attestation subject has no dogfood episodes")
    window = dogfood.get("observation_window")
    if not isinstance(window, dict) or window.get("status") != "eligible":
        raise ValueError("attestation subject dogfood window is not eligible")
    subjects = dogfood.get("attestation_subjects")
    if not isinstance(subjects, list) or not subjects:
        raise ValueError("attestation subject has no dogfood episode subjects")
    measures = subject.get("measures")
    if not isinstance(measures, dict) or any(
        not isinstance(measures.get(name), dict)
        or measures[name].get("status") != "observed"
        for name in MEASURE_NAMES
    ):
        raise ValueError("attestation subject does not contain all observed measures")


def _stable_measure(measure: dict[str, Any]) -> dict[str, Any]:
    stable = dict(measure)
    source = stable.get("source")
    if isinstance(source, list):
        stable["source"] = [
            value.get("sha256")
            for value in source
            if isinstance(value, dict) and isinstance(value.get("sha256"), str)
        ]
    return stable


def build_subject(report: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, externally attestable portion of a report."""

    if report.get("report_status") != "ok":
        raise ValueError("report is not valid for external qualification")
    provenance = report.get("provenance")
    inputs = report.get("inputs")
    dogfood = report.get("dogfood_series")
    if not isinstance(provenance, dict) or not isinstance(inputs, dict):
        raise ValueError("report has no provenance or input subject")
    if not isinstance(dogfood, dict):
        raise ValueError("report has no dogfood series subject")
    required_provenance = {
        "git_sha": provenance.get("git_sha"),
        "repository": REPOSITORY_IDENTITY,
        "repository_dirty": provenance.get("repository_dirty"),
        "harness_file_dirty": provenance.get("harness_file_dirty"),
        "sha256": provenance.get("sha256"),
    }
    if not isinstance(required_provenance["git_sha"], str) or len(required_provenance["git_sha"]) != 40:
        raise ValueError("report provenance has no full source SHA")
    if required_provenance["repository_dirty"] is not False or required_provenance["harness_file_dirty"] is not False:
        raise ValueError("report provenance is dirty")
    matrix = report.get("matrix")
    if isinstance(matrix, dict):
        for history in matrix.get("history", []):
            if not isinstance(history, dict):
                continue
            source_sha = history.get("implementation_source_git_sha")
            if source_sha is not None and source_sha != required_provenance["git_sha"]:
                raise ValueError("matrix artifact source SHA does not match the report")
    subjects = dogfood.get("attestation_subjects")
    if not isinstance(subjects, list) or not subjects:
        raise ValueError("report has no dogfood sampled-binary subjects")
    if dogfood.get("input_status") != "valid":
        raise ValueError("dogfood series input is not valid")
    observation_window = dogfood.get("observation_window")
    if not isinstance(observation_window, dict) or observation_window.get("status") != "eligible":
        raise ValueError("dogfood series does not meet its observation-window requirement")
    measures = report.get("measures")
    if not isinstance(measures, dict):
        raise ValueError("report has no measured dogfood values")
    measured_names = MEASURE_NAMES
    if any(
        not isinstance(measures.get(name), dict)
        or measures[name].get("status") != "observed"
        for name in measured_names
    ):
        raise ValueError("report does not contain all observed dogfood measures")
    subject = {
        "artifact_kind": "launch_reliability_measurements",
        "report_schema_version": report.get("schema_version"),
        "report_provenance": required_provenance,
        "inputs": {
            name: [
                value.get("sha256")
                for value in inputs.get(name, [])
                if isinstance(value, dict) and isinstance(value.get("sha256"), str)
            ]
            for name in (
                "matrix_artifacts",
                "provider_harness_artifacts",
                "health_artifacts",
                "dogfood_series_artifacts",
            )
        },
        "dogfood_series": {
            "input_status": dogfood.get("input_status"),
            "episode_count": dogfood.get("episode_count"),
            "observation_window": dogfood.get("observation_window"),
            "attestation_subjects": subjects,
        },
        "measures": {
            name: _stable_measure(measures[name]) for name in measured_names
        },
    }
    _validate_subject(subject)
    return subject


def _timestamp(raw: Any, *, label: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} must be an RFC3339 timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from exc
    if value.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _trusted_public_key(
    key_id: str, *, keys_path: Path | None = None
) -> Ed25519PublicKey:
    keys_path = keys_path or TRUSTED_KEYS_PATH
    payload = _read_object(keys_path, label="trusted attestation keys")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("trusted attestation keys have an unsupported schema version")
    keys = payload.get("keys")
    entry = keys.get(key_id) if isinstance(keys, dict) else None
    if not isinstance(entry, dict) or entry.get("algorithm") != ALGORITHM:
        raise ValueError(f"attestation key is not trusted: {key_id}")
    encoded = entry.get("public_key_base64")
    if not isinstance(encoded, str):
        raise ValueError(f"trusted attestation key has no public key: {key_id}")
    try:
        public_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"trusted attestation public key is not base64: {key_id}") from exc
    if len(public_bytes) != 32:
        raise ValueError(f"trusted attestation public key has the wrong length: {key_id}")
    return Ed25519PublicKey.from_public_bytes(public_bytes)


def _unsigned_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    unsigned = dict(receipt)
    unsigned.pop("signature", None)
    return unsigned


def create_receipt(
    subject_path: Path,
    private_key_path: Path,
    output_path: Path,
    *,
    key_id: str,
    now: datetime | None = None,
    keys_path: Path | None = None,
) -> dict[str, Any]:
    subject = _read_object(subject_path, label="attestation subject")
    _validate_subject(subject)
    try:
        key = serialization.load_pem_private_key(
            private_key_path.read_bytes(), password=None
        )
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("attestation private key is not a readable PEM key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("attestation private key must be Ed25519")
    trusted_key = _trusted_public_key(key_id, keys_path=keys_path)
    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    trusted_bytes = trusted_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if public_bytes != trusted_bytes:
        raise ValueError(f"attestation private key does not match trusted key: {key_id}")
    issued_at = (now or datetime.now(UTC)).astimezone(UTC)
    expires_at = issued_at + MAX_RECEIPT_LIFETIME
    receipt = {
        "artifact_kind": ARTIFACT_KIND,
        "algorithm": ALGORITHM,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "issuer": ISSUER,
        "key_id": key_id,
        "receipt_id": secrets.token_hex(16),
        "schema_version": SCHEMA_VERSION,
        "subject": subject,
        "subject_sha256": sha256_json(subject),
    }
    receipt["signature"] = base64.b64encode(
        key.sign(canonical_json(_unsigned_receipt(receipt)))
    ).decode("ascii")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def create_receipt_from_report(
    report_path: Path,
    private_key_path: Path,
    output_path: Path,
    subject_output_path: Path,
    *,
    key_id: str,
    keys_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive and sign a report subject in one release-owned operation."""

    report = _read_object(report_path, label="launch reliability report")
    subject = build_subject(report)
    subject_output_path.parent.mkdir(parents=True, exist_ok=True)
    subject_output_path.write_text(
        json.dumps(subject, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return create_receipt(
        subject_output_path,
        private_key_path,
        output_path,
        key_id=key_id,
        keys_path=keys_path,
        now=now,
    )


def verify_receipt(
    receipt_path: Path,
    subject: dict[str, Any],
    *,
    as_of: datetime | None = None,
    keys_path: Path | None = None,
) -> dict[str, Any]:
    receipt = _read_object(receipt_path, label="attestation receipt")
    if receipt.get("artifact_kind") != ARTIFACT_KIND:
        raise ValueError("attestation receipt has the wrong artifact kind")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("attestation receipt has the wrong schema version")
    if receipt.get("issuer") != ISSUER:
        raise ValueError("attestation receipt has the wrong issuer")
    if receipt.get("algorithm") != ALGORITHM:
        raise ValueError("attestation receipt has the wrong algorithm")
    key_id = receipt.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("attestation receipt has no key_id")
    receipt_id = receipt.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        raise ValueError("attestation receipt has no receipt_id")
    issued_at = _timestamp(receipt.get("issued_at"), label="attestation.issued_at")
    expires_at = _timestamp(receipt.get("expires_at"), label="attestation.expires_at")
    if expires_at <= issued_at or expires_at - issued_at > MAX_RECEIPT_LIFETIME:
        raise ValueError("attestation receipt lifetime is invalid")
    check_time = (as_of or datetime.now(UTC)).astimezone(UTC)
    if issued_at > check_time:
        raise ValueError("attestation receipt was issued in the future")
    if expires_at <= check_time:
        raise ValueError("attestation receipt has expired")
    expected_subject_hash = sha256_json(subject)
    if receipt.get("subject_sha256") != expected_subject_hash:
        raise ValueError("attestation receipt subject does not match the report inputs")
    if receipt.get("subject") != subject:
        raise ValueError("attestation receipt contains a different attestation subject")
    encoded_signature = receipt.get("signature")
    if not isinstance(encoded_signature, str):
        raise ValueError("attestation receipt has no signature")
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("attestation receipt signature is not base64") from exc
    if len(signature) != 64:
        raise ValueError("attestation receipt signature has the wrong length")
    try:
        _trusted_public_key(key_id, keys_path=keys_path).verify(
            signature, canonical_json(_unsigned_receipt(receipt))
        )
    except InvalidSignature as exc:
        raise ValueError("attestation receipt signature does not verify") from exc
    return {
        "path": str(receipt_path.resolve()),
        "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "receipt_id": receipt_id,
        "issuer": ISSUER,
        "key_id": key_id,
        "issued_at": receipt["issued_at"],
        "expires_at": receipt["expires_at"],
        "subject_sha256": expected_subject_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--subject", type=Path, required=True)
    create.add_argument("--private-key", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--key-id", required=True)
    create.add_argument("--keys", type=Path, default=TRUSTED_KEYS_PATH)
    create_from_report = subparsers.add_parser(
        "create-from-report",
        help="derive a qualifying subject from a report and sign it",
    )
    create_from_report.add_argument("--report", type=Path, required=True)
    create_from_report.add_argument("--private-key", type=Path, required=True)
    create_from_report.add_argument("--output", type=Path, required=True)
    create_from_report.add_argument("--subject-output", type=Path, required=True)
    create_from_report.add_argument("--key-id", required=True)
    create_from_report.add_argument("--keys", type=Path, default=TRUSTED_KEYS_PATH)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--subject", type=Path, required=True)
    verify.add_argument("--keys", type=Path, default=TRUSTED_KEYS_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            create_receipt(
                args.subject,
                args.private_key,
                args.output,
                key_id=args.key_id,
                keys_path=args.keys,
            )
            print(args.output)
        elif args.command == "create-from-report":
            create_receipt_from_report(
                args.report,
                args.private_key,
                args.output,
                args.subject_output,
                key_id=args.key_id,
                keys_path=args.keys,
            )
            print(args.output)
        else:
            subject = _read_object(args.subject, label="attestation subject")
            print(json.dumps(verify_receipt(args.receipt, subject, keys_path=args.keys), sort_keys=True))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"launch reliability attestation: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
