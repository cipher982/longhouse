"""Executable coordination scenarios that publish capability proof records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from zerg.mcp_server.server import COORDINATION_INSTRUCTIONS
from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.services.longhouse_paths import resolve_longhouse_home
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_capability_local_proof import executable_identity
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore
from zerg.services.shipper.hooks import CODEX_HOOK_SCRIPT

from .provider_capability_proof_publish import ScenarioProofIdentity
from .provider_capability_proof_publish import publish_scenario_assertions
from .provider_coordination_oracles import awareness_post_compaction_assertions


def observe_codex_post_compaction_bootstrap(*, compactions: int = 4) -> dict[str, object]:
    if shutil.which("jq") is None:
        raise RuntimeError("jq is required for the Codex coordination bootstrap scenario")
    with tempfile.TemporaryDirectory(prefix="longhouse-codex-coordination-") as raw_root:
        root = Path(raw_root)
        hook = root / "longhouse-codex-hook.sh"
        hook.write_text(
            CODEX_HOOK_SCRIPT.replace("__LONGHOUSE_HOME__", str(root / "longhouse")).replace("__ENGINE_PATH__", "/bin/true"),
            encoding="utf-8",
        )
        hook.chmod(0o755)
        env = os.environ.copy()
        env["LONGHOUSE_MANAGED_SESSION_ID"] = "11111111-1111-1111-1111-111111111111"
        visible_bootstrap_count = 0
        for _ in range(compactions):
            completed = subprocess.run(
                ["/bin/bash", str(hook)],
                input=json.dumps(
                    {
                        "hook_event_name": "SessionStart",
                        "source": "compact",
                        "session_id": "provider-session-id",
                        "cwd": str(root),
                        "transcript_path": str(root / "transcript.jsonl"),
                    }
                ),
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or f"Codex hook exited {completed.returncode}")
            visible_bootstrap_count += int(bool(completed.stdout.strip()))
    return {
        "coordination_instructions_model_visible_after_compaction": False,
        "visible_bootstrap_count": visible_bootstrap_count,
        "mcp_coordination_instructions_present": "`peers` tool" in COORDINATION_INSTRUCTIONS,
    }


def publish_codex_bootstrap_noise_proof(
    *,
    provider_version: str,
    provider_executable_identity: str,
    store: ProviderCapabilityProofStore,
    producer_class: str,
    producer_version: str,
    invocation_id: str,
    generated_at: str,
    observations: dict[str, object] | None = None,
    run_reference: str | None = None,
    raw_reference_digests: tuple[str, ...] = (),
    longhouse_git_sha: str | None = None,
) -> str:
    contract = contract_for_provider("codex")
    if contract is None:
        raise RuntimeError("Codex managed-provider contract is missing")
    declaration = contract.capabilities["coordination.awareness.post_compaction"]
    assertion = next(item for item in declaration["required_assertions"] if item["id"] == "no_duplicate_visible_bootstrap")
    observations = observations or observe_codex_post_compaction_bootstrap()
    assertions = awareness_post_compaction_assertions(observations)
    records = publish_scenario_assertions(
        identity=ScenarioProofIdentity(
            provider="codex",
            provider_version=provider_version,
            provider_executable_identity=provider_executable_identity,
            provider_contract_digest=contract.contract_entry_digest,
            adapter_digest=contract.adapter_digest,
            scenario_id=assertion["scenario_id"],
            scenario_revision=assertion["minimum_scenario_revision"],
            oracle_digest=assertion["oracle_digest"],
            evidence_class=EvidenceClass.HERMETIC,
            generated_at=generated_at,
            producer_class=producer_class,
            producer_version=producer_version,
            invocation_id=invocation_id,
            mode="helm",
            run_reference=run_reference,
            raw_reference_digests=raw_reference_digests,
            longhouse_git_sha=longhouse_git_sha,
        ),
        assertions={"no_duplicate_visible_bootstrap": assertions["no_duplicate_visible_bootstrap"]},
        store=store,
    )
    return records[0].artifact_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("codex",), default="codex")
    parser.add_argument("--provider-bin")
    parser.add_argument("--store-root")
    parser.add_argument("--producer-class", default="local_diagnostic")
    parser.add_argument("--producer-version", default="2")
    parser.add_argument("--invocation-id")
    parser.add_argument("--run-reference")
    parser.add_argument("--longhouse-git-sha")
    parser.add_argument("--provider-version")
    parser.add_argument("--provider-executable-identity")
    parser.add_argument("--bundle-output")
    args = parser.parse_args()
    provider_version = str(args.provider_version or "").strip()
    identity = str(args.provider_executable_identity or "").strip()
    if not provider_version or not identity:
        binary = Path(args.provider_bin or shutil.which(PROVIDER_CLI_BINARY_BY_PROVIDER[args.provider]) or "")
        identity = executable_identity(str(binary)) or ""
        if not identity:
            raise SystemExit(f"provider binary not found: {binary}")
        version_result = subprocess.run([str(binary), "--version"], text=True, capture_output=True, check=False)
        if version_result.returncode != 0 or not version_result.stdout.strip():
            raise SystemExit(version_result.stderr or "provider version probe failed")
        provider_version = version_result.stdout.strip()
    root = Path(args.store_root) if args.store_root else resolve_longhouse_home() / "provider-capability-proofs"
    observations = observe_codex_post_compaction_bootstrap()
    raw_payload = json.dumps(observations, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    artifact_id = publish_codex_bootstrap_noise_proof(
        provider_version=provider_version,
        provider_executable_identity=identity,
        store=ProviderCapabilityProofStore(root),
        producer_class=args.producer_class,
        producer_version=args.producer_version,
        invocation_id=args.invocation_id or str(uuid4()),
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        observations=observations,
        run_reference=args.run_reference,
        raw_reference_digests=(f"sha256:{hashlib.sha256(raw_payload).hexdigest()}",),
        longhouse_git_sha=args.longhouse_git_sha,
    )
    if args.bundle_output:
        store = ProviderCapabilityProofStore(root)
        record = next(record for record in store.records(args.provider) if record.artifact_id == artifact_id)
        bundle_path = Path(args.bundle_output)
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(
            json.dumps(
                {"artifact_kind": "provider_capability_proof_bundle", "records": [record.serialize()]},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        raw_path = bundle_path.with_suffix(".raw.json")
        raw_path.write_bytes(raw_payload)
    print(json.dumps({"provider": args.provider, "artifact_id": artifact_id, "store_root": str(root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
