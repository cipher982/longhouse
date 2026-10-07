#!/usr/bin/env python3
"""A workflow success alone must never authorize personal runtime promotion."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


class PromotionAuthorizationTests(unittest.TestCase):
    def run_promotion(
        self,
        *,
        receipt_available: bool = True,
        receipt_sha: str = SHA,
        gate_refuses: bool = False,
        final_attempt: int = 1,
        receipt_attempts: tuple[int, ...] = (1,),
        held_by_pid: int | None = None,
    ):
        with tempfile.TemporaryDirectory(prefix="longhouse-promotion-test-") as directory:
            root = Path(directory)
            ops = root / "scripts" / "ops"
            library = root / "scripts" / "lib"
            binaries = root / "bin"
            for path in (ops, library, binaries):
                path.mkdir(parents=True)
            for name in ("promote-dogfood.sh", "release-artifacts.py", "ring_lock.py"):
                shutil.copyfile(ROOT / "scripts" / "ops" / name, ops / name)
            shutil.copyfile(ROOT / "scripts" / "lib" / "ring-lock.sh", library / "ring-lock.sh")
            locks = root / "locks"
            if held_by_pid is not None:
                held = subprocess.run([sys.executable, str(ops / "ring_lock.py"), "acquire", "dogfood-fixture-owner",
                                       "--sha", "f" * 40, "--ttl", "600", "--pid", str(held_by_pid), "--op", "another agent"],
                                      env={**os.environ, "LONGHOUSE_RING_LOCK_DIR": str(locks)}, capture_output=True, text=True)
                assert held.returncode == 0, held.stderr
            receipt = {
                "schema": "longhouse.runtime-verification.v1",
                "source_sha": receipt_sha,
                "image_digest": DIGEST,
                "build_run_id": "10",
                "build_attempt": 1,
                "source_order": 10,
                "source_workflow": "Publish Runtime Image",
                "qualification_id": "runtime-image-10-1",
                "verification": {
                    "canary_deployment_id": "d-20260918-120000-canary",
                    "functional_smoke": "success",
                },
            }
            (root / "receipt.json").write_text(json.dumps(receipt))
            (library / "hosted-instance.sh").write_text(
                'lh_hosted_prepare_control_plane_auth() { :; }\n'
                'lh_hosted_resolve_instance() { LH_INSTANCE_ID=fixture; }\n'
                'lh_hosted_reprovision() { printf "%s\\n" "$2" >> "$FIXTURE_ROOT/promotions";'
                ' ls "$LONGHOUSE_RING_LOCK_DIR" | grep "\\\\.json$" >> "$FIXTURE_ROOT/locks_during" || true; }\n'
            )
            # The review gate has its own tests (review-gate.test.py); here it only has to be
            # asked, with the target and the served-health URL, before anything changes.
            (library / "review-gate.sh").write_text(
                'lh_review_gate_promotion() {\n'
                '  printf "%s %s\\n" "$1" "$2" >> "$FIXTURE_ROOT/gate_calls"\n'
                '  [[ "$FIXTURE_GATE_REFUSES" != 1 ]] || { echo "review-gate: REFUSED" >&2; return 1; }\n'
                '  echo "review-gate: promotion OK."\n'  # the real gate reports success on stdout
                '}\n'
            )
            # External services are isolated; the real receipt verifier and
            # complete promotion entrypoint still make the authorization decision.
            stub = f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys, zipfile
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
sha = os.environ['FIXTURE_SHA']
if name == 'git':
    print(sha)
elif name == 'gh':
    if args[:2] == ['run', 'list']:
        runs = [
            {'headSha':sha,'databaseId':21,'workflowName':'Deploy and Verify','event':'push'},
            {'headSha':sha,'databaseId':20,'workflowName':'Deploy and Verify','event':'workflow_dispatch'},
        ]
        if '--event' in args:
            event = args[args.index('--event')+1]
            runs = [run for run in runs if run['event'] == event]
        print(json.dumps(runs))
    elif args[:2] == ['run', 'view']:
        publish = args[2] == '10'
        attempt = 1 if publish else int(os.environ['FIXTURE_FINAL_ATTEMPT'])
        print(json.dumps({'headSha':sha,'attempt':attempt,'number':10,'status':'completed',
                         'conclusion':'success','workflowName':'Publish Runtime Image' if publish else 'Deploy and Verify'}))
    elif args[0] == 'api':
        available = '/runs/20/' in args[1] and os.environ['FIXTURE_HAS_RECEIPT'] == '1'
        artifacts = []
        if available:
            for attempt in (int(n) for n in os.environ['FIXTURE_RECEIPT_ATTEMPTS'].split(',')):
                artifacts.append({'id':100+attempt,'expired':False,'name':f'runtime-verification-20-{attempt}'})
            # Artifacts that are not canary receipts never count, whatever their attempt suffix.
            artifacts.append({'id':900,'expired':False,'name':'hosted-live-qa-failure-20-1'})
            artifacts.append({'id':901,'expired':False,'name':'runtime-verification-20-x'})
        print(json.dumps({'artifacts':artifacts}))
    else:
        raise AssertionError(args)
elif name == 'curl':
    destination = args[args.index('--output')+1]
    with open(root/'downloads', 'a') as log:
        log.write(args[-1].rsplit('/artifacts/', 1)[1].split('/')[0] + '\n')
    with zipfile.ZipFile(destination, 'w') as archive:
        archive.write(root/'receipt.json','runtime-verification.json')
elif name == 'unzip':
    with zipfile.ZipFile(args[1]) as archive:
        sys.stdout.buffer.write(archive.read(args[2]))
elif name == 'python3':
    if len(args) > 1 and args[1] == 'inspect':
        print(json.dumps({'source_sha':sha,'schema_version':1,'schema_min_reader':1,'schema_max_reader':1}))
    else:
        os.execv(os.environ['FIXTURE_PYTHON'], [os.environ['FIXTURE_PYTHON'], *args])
else:
    raise AssertionError(name)
'''
            for name in ("git", "gh", "curl", "unzip", "python3"):
                path = binaries / name
                path.write_text(stub)
                path.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{binaries}:{os.environ['PATH']}",
                "FIXTURE_ROOT": str(root),
                "FIXTURE_SHA": SHA,
                "FIXTURE_HAS_RECEIPT": "1" if receipt_available else "0",
                "FIXTURE_FINAL_ATTEMPT": str(final_attempt),
                "FIXTURE_RECEIPT_ATTEMPTS": ",".join(str(n) for n in receipt_attempts),
                "FIXTURE_PYTHON": sys.executable,
                "FIXTURE_GATE_REFUSES": "1" if gate_refuses else "0",
                "SUBDOMAIN": "fixture-owner",
                "GH_TOKEN": "fixture-not-a-credential",
                "LONGHOUSE_RING_LOCK_DIR": str(locks),
            }
            result = subprocess.run(
                ["bash", str(ops / "promote-dogfood.sh"), SHA],
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
            )
            promotions = root / "promotions"
            gate_calls = root / "gate_calls"
            downloads = root / "downloads"
            self.gate_calls = gate_calls.read_text().splitlines() if gate_calls.exists() else []
            self.downloaded_artifacts = downloads.read_text().splitlines() if downloads.exists() else []
            during = root / "locks_during"
            self.locks_during = during.read_text().split() if during.exists() else []
            self.locks_left = sorted(p.name for p in locks.glob("*.json")) if locks.exists() else []
            events = locks / "events.jsonl"
            self.lock_events = [json.loads(line)["event"] for line in events.read_text().splitlines()] if events.exists() else []
            return result, promotions.read_text().splitlines() if promotions.exists() else []

    def test_manual_canary_receipt_survives_newer_successful_noop(self):
        result, promotions = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])

    def test_review_gate_is_asked_for_the_target_against_what_dogfood_serves(self):
        result, _ = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gate_calls, [f"{SHA} https://fixture-owner.longhouse.ai/api/health"])

    def test_the_gates_success_line_stays_off_stdout(self):
        # Like promote-production: stdout is for what the script produces, not the gate's chatter.
        result, _ = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("review-gate:", result.stdout)
        self.assertIn("review-gate: promotion OK.", result.stderr)

    def test_the_ring_lock_is_held_while_it_promotes_and_released_after(self):
        result, promotions = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        self.assertEqual(self.locks_during, ["dogfood-fixture-owner.json"])
        self.assertEqual(self.locks_left, [])

    def test_a_promotion_in_flight_refuses_a_second_before_the_gates_run(self):
        result, promotions = self.run_promotion(held_by_pid=os.getpid())
        self.assertEqual(result.returncode, 1)
        self.assertEqual((promotions, self.gate_calls), ([], []))
        self.assertIn("ring-lock: REFUSED: dogfood-fixture-owner held by", result.stderr)
        self.assertIn("could not take the fixture-owner promotion lock", result.stderr)
        self.assertEqual(self.locks_left, ["dogfood-fixture-owner.json"])

    def test_a_lock_whose_holder_died_is_reclaimed_and_the_reclaim_logged(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        result, promotions = self.run_promotion(held_by_pid=dead.pid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        self.assertIn("ring-lock: reclaimed dogfood-fixture-owner from", result.stderr)
        self.assertEqual(self.lock_events, ["acquired", "reclaimed", "acquired", "released"])

    def test_an_unreviewed_range_promotes_nothing(self):
        result, promotions = self.run_promotion(gate_refuses=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])
        self.assertIn("REFUSED", result.stderr)

    def test_receipt_from_an_earlier_attempt_counts_when_the_run_finally_succeeded(self):
        # `gh run rerun --failed` reran only the demo job: the canary receipt was uploaded by
        # attempt 1, and the run's final conclusion (attempt 2) is success.
        result, promotions = self.run_promotion(final_attempt=2, receipt_attempts=(1,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])
        self.assertEqual(self.downloaded_artifacts, ["101"])

    def test_newest_attempt_receipt_wins_when_several_attempts_uploaded_one(self):
        result, _ = self.run_promotion(final_attempt=3, receipt_attempts=(1, 3, 2))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.downloaded_artifacts, ["103"])

    def test_receipt_from_an_attempt_after_the_final_one_is_not_used(self):
        result, promotions = self.run_promotion(final_attempt=1, receipt_attempts=(2,))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])
        self.assertEqual(self.downloaded_artifacts, [])

    def test_an_earlier_attempt_receipt_for_another_source_cannot_promote(self):
        result, promotions = self.run_promotion(final_attempt=2, receipt_attempts=(1,), receipt_sha="c" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])

    def test_successful_workflows_without_receipts_cannot_promote(self):
        result, promotions = self.run_promotion(receipt_available=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])

    def test_receipt_for_another_source_cannot_promote(self):
        result, promotions = self.run_promotion(receipt_sha="c" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])


if __name__ == "__main__":
    unittest.main()
