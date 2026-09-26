#!/usr/bin/env python3
"""Production promotion must trust only a soaked, healthy dogfood deployment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE_REF = f"ghcr.io/cipher982/longhouse-runtime@{DIGEST}"
VERSION = "v9.9.9"
DOGFOOD_SUBDOMAIN = "fixture-dogfood"
DOGFOOD_HEALTH_URL = "https://fixture-dogfood.test/api/health"
DEMO_HEALTH_URL = "https://fixture-demo.test/api/health"


def _iso(hours_ago: float) -> str:
    # The control plane's real shape: UTC with no offset, microseconds included.
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None).isoformat()


def _deployments_json(rows: list[dict]) -> str:
    return json.dumps({"deployments": rows})


def _soaked_row(*, hours_ago: float = 30, status: str = "success", source_sha: str = SHA) -> dict:
    return {
        "id": "d-fixture-1",
        "image": IMAGE_REF,
        "image_digest": IMAGE_REF,
        "status": status,
        "submission_key": f"promote-dogfood-{DOGFOOD_SUBDOMAIN}-{SHA}",
        "completed_at": _iso(hours_ago),
        "source_sha": source_sha,
    }


CURL_STUB = f'''#!{sys.executable}
import json
import os
import pathlib
import sys

args = sys.argv[1:]
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
has_o = "-o" in args
url = args[-1]

if has_o:
    out_path = args[args.index("-o") + 1]
    stripped = url.rstrip("/")
    if stripped.endswith("/api/deployments"):
        body = os.environ.get("FIXTURE_DEPLOYMENTS_JSON", json.dumps({{"deployments": []}}))
    elif stripped.endswith("/api/instances"):
        body = os.environ.get("FIXTURE_INSTANCES_JSON", json.dumps({{"instances": []}}))
    else:
        raise AssertionError(f"unexpected -o curl url: {{url}}")
    pathlib.Path(out_path).write_text(body)
    sys.stdout.write("200")
else:
    if url == os.environ.get("FIXTURE_DOGFOOD_HEALTH_URL"):
        print(os.environ.get("FIXTURE_DOGFOOD_HEALTH_JSON", json.dumps({{"status": "healthy"}})))
    elif url == os.environ.get("FIXTURE_DEMO_HEALTH_URL"):
        print(os.environ.get("FIXTURE_DEMO_HEALTH_JSON", "{{}}"))
    else:
        raise AssertionError(f"unexpected curl url: {{url}}")
'''

GIT_STUB = f'''#!{sys.executable}
import os
import sys

args = sys.argv[1:]
if "ls-remote" in args:
    if os.environ.get("FIXTURE_TAG_EXISTS", "1") == "1":
        ref = next((a for a in args if a.startswith("refs/tags/") and not a.endswith("^{{}}")), None)
        if ref:
            print(f"{{os.environ['FIXTURE_SHA']}}\\t{{ref}}")
else:
    raise AssertionError(args)
'''

GH_STUB = f'''#!{sys.executable}
import json
import os
import sys

args = sys.argv[1:]
if args[:2] == ["release", "view"]:
    sys.exit(0 if os.environ.get("FIXTURE_HAS_RELEASE", "1") == "1" else 1)
elif args[:2] == ["run", "list"]:
    print(json.dumps([
        {{
            "databaseId": 123,
            "number": 45,
            "attempt": 1,
            "headSha": os.environ["FIXTURE_SHA"],
            "workflowName": "Publish Runtime Image",
            "conclusion": "success",
        }}
    ]))
else:
    raise AssertionError(args)
'''

SSH_STUB = f'''#!{sys.executable}
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ["FIXTURE_ROOT"])
with open(root / "ssh_invocations", "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
'''


class PromoteProductionTests(unittest.TestCase):
    def run_promotion(
        self,
        *,
        tag_exists: bool = True,
        has_release: bool = True,
        deployment_rows: list[dict] | None = None,
        instance_entries: list[dict] | None = None,
        dogfood_status: str = "healthy",
        demo_commit: str | None = None,
    ):
        if deployment_rows is None:
            deployment_rows = [_soaked_row()]
        if instance_entries is None:
            instance_entries = [
                {"id": 11, "status": "active", "subdomain": "acme"},
                {"id": 12, "status": "provisioning", "subdomain": "other"},
                {"id": 99, "status": "active", "subdomain": DOGFOOD_SUBDOMAIN},
            ]
        if demo_commit is None:
            demo_commit = SHA

        with tempfile.TemporaryDirectory(prefix="longhouse-promote-production-test-") as directory:
            root = Path(directory)
            ops = root / "scripts" / "ops"
            library = root / "scripts" / "lib"
            binaries = root / "bin"
            for path in (ops, library, binaries):
                path.mkdir(parents=True)

            shutil.copyfile(ROOT / "scripts" / "ops" / "promote-production.sh", ops / "promote-production.sh")
            (ops / "promote-production.sh").chmod(0o755)

            # The image-inspection / build-identity / submit+wait mechanics are
            # already exercised by promote-dogfood.test.py via the real
            # lh_hosted_reprovision. This test isolates promote-production.sh's
            # own orchestration (soak, health, targets, demo pin) by faking the
            # shared library's control-plane auth and production submission.
            (library / "hosted-instance.sh").write_text(
                'lh_hosted_prepare_control_plane_auth() {\n'
                '  CONTROL_PLANE_URL="https://control.fixture.test"\n'
                '  CONTROL_PLANE_ADMIN_TOKEN="fixture-token"\n'
                '  export CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN\n'
                '}\n'
                'lh_hosted_reprovision_production() {\n'
                '  printf "%s\\t%s\\n" "$1" "$2" >> "$FIXTURE_ROOT/promotions"\n'
                '}\n'
            )

            for name, content in (("git", GIT_STUB), ("gh", GH_STUB), ("curl", CURL_STUB), ("ssh", SSH_STUB)):
                path = binaries / name
                path.write_text(content)
                path.chmod(0o755)

            environment = {
                **os.environ,
                "PATH": f"{binaries}:{os.environ['PATH']}",
                "FIXTURE_ROOT": str(root),
                "FIXTURE_SHA": SHA,
                "FIXTURE_TAG_EXISTS": "1" if tag_exists else "0",
                "FIXTURE_HAS_RELEASE": "1" if has_release else "0",
                "FIXTURE_DEPLOYMENTS_JSON": _deployments_json(deployment_rows),
                "FIXTURE_INSTANCES_JSON": json.dumps({"instances": instance_entries}),
                "FIXTURE_DOGFOOD_HEALTH_URL": DOGFOOD_HEALTH_URL,
                "FIXTURE_DOGFOOD_HEALTH_JSON": json.dumps({"status": dogfood_status}),
                "FIXTURE_DEMO_HEALTH_URL": DEMO_HEALTH_URL,
                "FIXTURE_DEMO_HEALTH_JSON": json.dumps({"build": {"commit": demo_commit}}),
                "SUBDOMAIN": DOGFOOD_SUBDOMAIN,
                "DOGFOOD_HEALTH_URL": DOGFOOD_HEALTH_URL,
                "DEMO_HEALTH_URL": DEMO_HEALTH_URL,
                "DEMO_VERIFY_TIMEOUT": "5",
                "CONTROL_PLANE_ADMIN_TOKEN": "fixture-not-a-credential",
                "GH_TOKEN": "fixture-not-a-credential",
            }
            result = subprocess.run(
                ["bash", str(ops / "promote-production.sh"), VERSION],
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            promotions_path = root / "promotions"
            promotions = promotions_path.read_text().splitlines() if promotions_path.exists() else []
            ssh_path = root / "ssh_invocations"
            ssh_calls = ssh_path.read_text().splitlines() if ssh_path.exists() else []
            return result, promotions, ssh_calls

    def test_happy_path_with_targets(self) -> None:
        result, promotions, ssh_calls = self.run_promotion()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        image_ref, target_ids_json = promotions[0].split("\t")
        self.assertEqual(image_ref, IMAGE_REF)
        self.assertEqual(json.loads(target_ids_json), [11])
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn("targets=1", result.stdout)
        self.assertIn("demo_verified=true", result.stdout)

    def test_pointer_only_with_zero_targets(self) -> None:
        result, promotions, ssh_calls = self.run_promotion(
            instance_entries=[{"id": 99, "status": "active", "subdomain": DOGFOOD_SUBDOMAIN}]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        image_ref, target_ids_json = promotions[0].split("\t")
        self.assertEqual(image_ref, IMAGE_REF)
        self.assertEqual(json.loads(target_ids_json), [])
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn("targets=0", result.stdout)

    def test_soak_too_young_is_refused(self) -> None:
        result, promotions, ssh_calls = self.run_promotion(deployment_rows=[_soaked_row(hours_ago=2)])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])
        self.assertEqual(ssh_calls, [])
        self.assertIn("soak", result.stderr.lower())

    def test_missing_release_is_refused(self) -> None:
        result, promotions, ssh_calls = self.run_promotion(has_release=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])
        self.assertEqual(ssh_calls, [])
        self.assertIn("release", result.stderr.lower())

    def test_dogfood_unhealthy_is_refused(self) -> None:
        result, promotions, ssh_calls = self.run_promotion(dogfood_status="degraded")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(promotions, [])
        self.assertEqual(ssh_calls, [])
        self.assertIn("healthy", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
