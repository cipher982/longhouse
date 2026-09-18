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
    def run_promotion(self, *, receipt_available: bool = True, receipt_sha: str = SHA):
        with tempfile.TemporaryDirectory(prefix="longhouse-promotion-test-") as directory:
            root = Path(directory)
            ops = root / "scripts" / "ops"
            library = root / "scripts" / "lib"
            binaries = root / "bin"
            for path in (ops, library, binaries):
                path.mkdir(parents=True)
            for name in ("promote-dogfood.sh", "release-artifacts.py"):
                shutil.copyfile(ROOT / "scripts" / "ops" / name, ops / name)
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
                'lh_hosted_reprovision() { printf "%s\\n" "$2" >> "$FIXTURE_ROOT/promotions"; }\n'
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
        print(json.dumps({'headSha':sha,'attempt':1,'number':10,'status':'completed',
                         'conclusion':'success','workflowName':'Publish Runtime Image' if publish else 'Deploy and Verify'}))
    elif args[0] == 'api':
        available = '/runs/20/' in args[1] and os.environ['FIXTURE_HAS_RECEIPT'] == '1'
        print(json.dumps({'artifacts':[{'id':120,'expired':False,'name':'runtime-verification-20-1'}] if available else []}))
    else:
        raise AssertionError(args)
elif name == 'curl':
    destination = args[args.index('--output')+1]
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
                "FIXTURE_PYTHON": sys.executable,
                "SUBDOMAIN": "fixture-owner",
                "GH_TOKEN": "fixture-not-a-credential",
            }
            result = subprocess.run(
                ["bash", str(ops / "promote-dogfood.sh"), SHA],
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
            )
            promotions = root / "promotions"
            return result, promotions.read_text().splitlines() if promotions.exists() else []

    def test_manual_canary_receipt_survives_newer_successful_noop(self):
        result, promotions = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"])

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
