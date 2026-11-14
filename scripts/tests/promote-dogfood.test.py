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
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_receipts  # noqa: E402
import fake_s3  # noqa: E402
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


class PromotionAuthorizationTests(unittest.TestCase):
    def run_promotion(
        self,
        *,
        receipt_available: bool = True,
        receipt_sha: str = SHA,
        final_attempt: int = 1,
        receipt_attempts: tuple[int, ...] = (1,),
        held_by_pid: int | None = None,
        fast_lane: bool = False,
        served: str = "d" * 40,
        sensitive_rc: int = 0,
        ancestor: bool = True,
        live_preview: str = "",
        statuses: list | None = None,
        status_unreadable: bool = False,
        health_unreadable: bool = False,
    ):
        s3 = fake_s3.FakeS3()
        s3.__enter__()
        self.addCleanup(s3.__exit__)
        with tempfile.TemporaryDirectory(prefix="longhouse-promotion-test-") as directory:
            root = Path(directory)
            ops = root / "scripts" / "ops"
            library = root / "scripts" / "lib"
            binaries = root / "bin"
            for path in (ops, library, binaries):
                path.mkdir(parents=True)
            for name in ("promote-dogfood.sh", "release-artifacts.py", "ci_receipts.py", "ring_lock.py"):
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
            if fast_lane:
                receipt = {key: value for key, value in receipt.items() if key != "verification"}
                receipt["schema"] = "longhouse.runtime-publication.v1"
            (root / "receipt.json").write_text(json.dumps(receipt))
            # CI receipts, as the producers record them (scripts/ops/ci_receipts.py).
            if fast_lane:
                seeded = [("runtime-publication", "10", 10, 1, "runtime-publication.json")]
            elif receipt_available:
                seeded = [("runtime-verification", "20", 20, attempt, "runtime-verification.json") for attempt in receipt_attempts]
            else:
                seeded = []
            for kind, subject, run_id, attempt, member in seeded:
                base = f"{fake_s3.PREFIX}/{kind}/{subject}/{run_id}-{attempt}"
                s3.objects[f"{base}/{member}"] = json.dumps(receipt).encode()
                s3.objects[f"{base}/_receipt.json"] = json.dumps({
                    "schema": ci_receipts.STAMP_SCHEMA, "kind": kind, "subject": subject, "run_id": run_id,
                    "run_attempt": attempt, "repository": "o/r", "workflow_ref": "", "sha": receipt_sha,
                    "files": [member], "written_at": f"2026-09-18T12:0{attempt}:00+00:00",
                }).encode()
            (library / "hosted-instance.sh").write_text(
                'lh_hosted_prepare_control_plane_auth() { :; }\n'
                'lh_hosted_resolve_instance() { LH_INSTANCE_ID=fixture; }\n'
                'lh_hosted_reprovision() { printf "%s\\n" "$2" >> "$FIXTURE_ROOT/promotions";'
                ' ls "$LONGHOUSE_RING_LOCK_DIR" | grep "\\\\.json$" >> "$FIXTURE_ROOT/locks_during" || true; }\n'
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
    if 'merge-base' in args:
        sys.exit(0 if os.environ['FIXTURE_ANCESTOR'] == '1' else 1)
    print(sha)
elif name == 'gh':
    if args[:2] == ['run', 'list'] and 'Publish Runtime Image' in args:
        with open(root/'publish_lookups', 'a') as log:
            log.write('x\n')
        print(json.dumps([{'headSha':sha,'databaseId':10,'attempt':1,'event':'push'}]))
    elif args[:2] == ['run', 'list']:
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
    elif args[:1] == ['api']:
        with open(root/'status_lookups', 'a') as log:
            log.write(args[1] + '\n')
        if os.environ['FIXTURE_STATUS_UNREADABLE'] == '1':
            sys.exit(1)
        print(json.dumps(json.loads(os.environ['FIXTURE_STATUSES'])))
    else:
        raise AssertionError(args)
elif name == 'curl' and args[-1].endswith('/api/health'):
    if os.environ['FIXTURE_HEALTH_UNREADABLE'] == '1':
        sys.exit(22)
    print(json.dumps({'build': {'commit': os.environ['FIXTURE_SERVED']}}))
elif name == 'python3':
    if args and args[0].endswith('dogfood_sensitive_paths.py') and 'live-preview' in args:
        with open(root/'live_preview_calls', 'a') as log:
            log.write(f"{args[args.index('--base') + 1]} {args[args.index('--head') + 1]}\n")
        if os.environ['FIXTURE_LIVE_PREVIEW'] == 'undecidable':
            sys.exit(2)
        print(os.environ['FIXTURE_LIVE_PREVIEW'] if os.environ['FIXTURE_LIVE_PREVIEW'] else '', end='')
        sys.exit(0)
    if args and args[0].endswith('dogfood_sensitive_paths.py'):
        with open(root/'sensitive_calls', 'a') as log:
            log.write(f"{args[args.index('--base') + 1]} {args[args.index('--head') + 1]}\n")
        sys.exit(int(os.environ['FIXTURE_SENSITIVE']))
    if len(args) > 1 and args[1] == 'inspect':
        print(json.dumps({'source_sha':sha,'schema_version':1,'schema_min_reader':1,'schema_max_reader':1}))
    else:
        os.execv(os.environ['FIXTURE_PYTHON'], [os.environ['FIXTURE_PYTHON'], *args])
else:
    raise AssertionError(name)
'''
            for name in ("git", "gh", "curl", "python3"):
                path = binaries / name
                path.write_text(stub)
                path.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{binaries}:{os.environ['PATH']}",
                "FIXTURE_ROOT": str(root),
                "FIXTURE_SHA": SHA,
                "FIXTURE_FINAL_ATTEMPT": str(final_attempt),
                "FIXTURE_PYTHON": sys.executable,
                "SUBDOMAIN": "fixture-owner",
                "GH_TOKEN": "fixture-not-a-credential",
                "LONGHOUSE_RING_LOCK_DIR": str(locks),
                "FIXTURE_SERVED": served,
                "FIXTURE_SENSITIVE": str(sensitive_rc),
                "FIXTURE_ANCESTOR": "1" if ancestor else "0",
                "FIXTURE_LIVE_PREVIEW": live_preview,
                "FIXTURE_STATUSES": json.dumps(statuses if statuses is not None else []),
                "FIXTURE_STATUS_UNREADABLE": "1" if status_unreadable else "0",
                "FIXTURE_HEALTH_UNREADABLE": "1" if health_unreadable else "0",
                **s3.env(),
            }
            result = subprocess.run(
                ["bash", str(ops / "promote-dogfood.sh"), *(["--fast-lane"] if fast_lane else []), SHA],
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
            )
            promotions = root / "promotions"
            # The receipt instances the script actually read (run-attempt), in order.
            self.read_receipts = [
                key.split("/")[-2] for key in s3.reads if key.endswith(("runtime-verification.json", "runtime-publication.json"))
            ]
            during = root / "locks_during"
            self.locks_during = during.read_text().split() if during.exists() else []
            sensitive_calls = root / "sensitive_calls"
            self.sensitive_calls = sensitive_calls.read_text().splitlines() if sensitive_calls.exists() else []
            self.publish_lookups = (root / "publish_lookups").exists()
            self.live_preview_calls = (root / "live_preview_calls").read_text().splitlines() if (root / "live_preview_calls").exists() else []
            self.status_lookups = (root / "status_lookups").read_text().splitlines() if (root / "status_lookups").exists() else []
            self.locks_left = sorted(p.name for p in locks.glob("*.json")) if locks.exists() else []
            events = locks / "events.jsonl"
            self.lock_events = [json.loads(line)["event"] for line in events.read_text().splitlines()] if events.exists() else []
            return result, promotions.read_text().splitlines() if promotions.exists() else []

    def test_canary_qualified_path_uses_its_exact_canary_receipt(self):
        result, promotions = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])
        self.assertEqual(self.sensitive_calls, [])

    def test_the_ring_lock_is_held_while_it_promotes_and_released_after(self):
        result, promotions = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        self.assertEqual(self.locks_during, ["dogfood-fixture-owner.json"])
        self.assertEqual(self.locks_left, [])

    def test_a_promotion_in_flight_refuses_a_second_before_the_gates_run(self):
        result, promotions = self.run_promotion(held_by_pid=os.getpid())
        self.assertEqual(result.returncode, 1)
        self.assertEqual((promotions, self.sensitive_calls), ([], []))
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


    def test_receipt_from_an_earlier_attempt_counts_when_the_run_finally_succeeded(self):
        # `gh run rerun --failed` reran only the demo job: the canary receipt was uploaded by
        # attempt 1, and the run's final conclusion (attempt 2) is success.
        result, promotions = self.run_promotion(final_attempt=2, receipt_attempts=(1,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])
        self.assertEqual(self.read_receipts, ["20-1"])

    def test_newest_attempt_receipt_wins_when_several_attempts_uploaded_one(self):
        result, _ = self.run_promotion(final_attempt=3, receipt_attempts=(1, 3, 2))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_receipts, ["20-3"])

    def test_receipt_from_an_attempt_after_the_final_one_is_not_used(self):
        result, promotions = self.run_promotion(final_attempt=1, receipt_attempts=(2,))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])
        self.assertEqual(self.read_receipts, [])

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


    # --- the fast lane: published image, no sensitive paths since what dogfood serves ---

    def test_fast_lane_promotes_the_published_image_when_the_range_is_not_sensitive(self):
        result, promotions = self.run_promotion(fast_lane=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])
        self.assertEqual(self.sensitive_calls, [f"{'d' * 40} {SHA}"])
        self.assertEqual(self.read_receipts, ["10-1"])  # the publication receipt of the publishing attempt
        self.assertIn("(fast-lane)", result.stdout)
        self.assertEqual(self.locks_during, ["dogfood-fixture-owner.json"])
        self.assertEqual(self.locks_left, [])

    def test_fast_lane_hands_a_sensitive_range_to_the_qualified_path(self):
        result, promotions = self.run_promotion(fast_lane=True, sensitive_rc=1)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(promotions, [])
        self.assertFalse(self.publish_lookups)
        self.assertIn("needs the canary-qualified path", result.stderr)

    def test_fast_lane_refuses_when_the_classifier_cannot_decide(self):
        result, promotions = self.run_promotion(fast_lane=True, sensitive_rc=2)
        self.assertEqual((result.returncode, promotions), (1, []))

    def test_fast_lane_never_moves_dogfood_backwards_or_sideways(self):
        result, promotions = self.run_promotion(fast_lane=True, ancestor=False)
        self.assertEqual((result.returncode, promotions), (1, []))
        self.assertIn("never moves dogfood backwards", result.stderr)
        self.assertEqual(self.sensitive_calls, [])

    def test_fast_lane_is_a_no_op_when_dogfood_already_serves_it(self):
        result, promotions = self.run_promotion(fast_lane=True, served=SHA)
        self.assertEqual((result.returncode, promotions), (0, []))
        self.assertIn("already serves", result.stdout)

    def test_fast_lane_receipt_for_another_source_cannot_promote(self):
        result, promotions = self.run_promotion(fast_lane=True, receipt_sha="c" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])

    # --- the terminal-truth requirement: a live-preview change needs its passing commit status ---

    LP = "f1" * 20  # the newest commit in the range that touches a live-preview path
    PASSED = {"context": "longhouse/terminal-truth", "state": "success", "creator": {"login": "github-actions[bot]"},
              "target_url": "https://github.com/cipher982/longhouse/actions/runs/1"}

    def status(self, **over):
        return {**self.PASSED, **over}

    def test_a_live_preview_change_with_a_passing_status_promotes(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.PASSED])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])
        self.assertEqual(self.live_preview_calls, [f"{'d' * 40} {SHA}"])
        self.assertEqual(self.status_lookups, [f"repos/cipher982/longhouse/commits/{self.LP}/statuses?per_page=100"])
        self.assertIn("passed", result.stdout)

    def test_a_live_preview_change_with_no_status_waits_and_promotes_nothing(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[])
        self.assertEqual((result.returncode, promotions), (4, []))
        self.assertIn("has no terminal-truth status yet", result.stderr)
        self.assertEqual(self.read_receipts, [])  # refused before any canary receipt was read
        self.assertEqual(self.locks_left, [])

    def test_another_context_does_not_count_as_the_terminal_truth_status(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.status(context="longhouse/dogfood")])
        self.assertEqual((result.returncode, promotions), (4, []))

    def test_a_pending_status_waits(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.status(state="pending")])
        self.assertEqual((result.returncode, promotions), (4, []))
        self.assertIn("still pending", result.stderr)

    def test_a_failed_status_refuses(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.status(state="failure")])
        self.assertEqual((result.returncode, promotions), (5, []))
        self.assertIn("is failure", result.stderr)

    def test_an_errored_status_refuses(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.status(state="error")])
        self.assertEqual((result.returncode, promotions), (5, []))

    def test_a_passing_status_not_posted_by_github_actions_refuses(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.status(creator={"login": "someone"})])
        self.assertEqual((result.returncode, promotions), (5, []))
        self.assertIn("not GitHub Actions", result.stderr)

    def test_the_newest_status_decides_when_there_are_several(self):
        # GitHub lists statuses newest first: a success above an older pending promotes, a failure above an older success refuses
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.PASSED, self.status(state="pending")])
        self.assertEqual(result.returncode, 0, result.stderr)
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[self.status(state="failure"), self.PASSED])
        self.assertEqual((result.returncode, promotions), (5, []))

    def test_an_unreadable_status_waits(self):
        result, promotions = self.run_promotion(live_preview=self.LP, status_unreadable=True)
        self.assertEqual((result.returncode, promotions), (4, []))

    def test_a_range_without_live_preview_paths_needs_no_status_and_makes_no_lookup(self):
        result, promotions = self.run_promotion(live_preview="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        self.assertEqual(self.status_lookups, [])
        self.assertIn("no terminal-truth status needed", result.stdout)

    def test_when_the_classifier_cannot_decide_nothing_is_promoted(self):
        result, promotions = self.run_promotion(live_preview="undecidable")
        self.assertEqual((result.returncode, promotions), (1, []))

    def test_by_hand_with_dogfood_unreadable_the_check_is_skipped_loudly(self):
        result, promotions = self.run_promotion(live_preview=self.LP, statuses=[], health_unreadable=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        self.assertIn("terminal-truth status was NOT checked", result.stderr)

    def test_a_sensitive_range_is_handed_to_the_qualified_path_before_any_status_is_asked_for(self):
        result, promotions = self.run_promotion(fast_lane=True, sensitive_rc=1, live_preview=self.LP, statuses=[])
        self.assertEqual((result.returncode, promotions), (3, []))
        self.assertEqual(self.status_lookups, [])

    def test_the_fast_lane_also_needs_the_status_and_waits_for_it(self):
        result, promotions = self.run_promotion(fast_lane=True, live_preview=self.LP, statuses=[])
        self.assertEqual((result.returncode, promotions), (4, []))
        result, promotions = self.run_promotion(fast_lane=True, live_preview=self.LP, statuses=[self.status(state="failure")])
        self.assertEqual((result.returncode, promotions), (5, []))
        result, promotions = self.run_promotion(fast_lane=True, live_preview=self.LP, statuses=[self.PASSED])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])


if __name__ == "__main__":
    unittest.main()
