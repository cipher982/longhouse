"""A fixture world for the production promotion tests.

`green_world()` is everything the gates read when a promotion is allowed: dogfood
serving the commit, a passed Hosted Live QA receipt, a passed engine-compat
receipt, a sealed OCI archive receipt, and a control plane with no real tenants. Each test breaks one thing.
The same dict is served two ways: in-process (`InProcessSources`, for the gate
tests) and over HTTP plus stubbed `gh`/`ssh` (`Wire`, for the script tests).
"""

from __future__ import annotations

import copy
import io
import json
import os
import stat
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
import promotion_gates as gates  # noqa: E402

SHA = "a" * 40
OTHER_SHA = "d" * 40
DIGEST = "ghcr.io/cipher982/longhouse-runtime@sha256:" + "b" * 64
OTHER_DIGEST = "ghcr.io/cipher982/longhouse-runtime@sha256:" + "e" * 64
DOGFOOD = "fixture-dogfood"
REPO = "cipher982/longhouse"
QA_RUN = 36000000001
COMPAT_RUN = 36000000002
ARCHIVE_RUN = 36000000003


def qa_receipt(*, verdict: str = "passed", sha: str = SHA, run: int = QA_RUN, digest: str | None = DIGEST, failed_steps=None) -> dict:
    return {
        "schema": gates.QA_SCHEMA,
        "verdict": verdict,
        "reason": f"fixture verdict {verdict}",
        "verified_sha": sha,
        "canary_sha": sha,
        "canary_deployment_id": "d-canary",
        "canary_image_digest": digest,
        "decided_at": "2026-09-29T23:00:00Z",
        "failed_steps": failed_steps or [],
        "run": {"id": str(run), "attempt": "1"},
    }


def qa_run(**changes: Any) -> dict:
    """The GitHub run of the Hosted Live QA workflow that uploaded the receipt: main's workflow file, dispatched on main."""
    return {
        "id": QA_RUN,
        "name": "Hosted Live QA",
        "path": gates.QA_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": "c" * 40,
        "head_repository": {"full_name": REPO},
        "repository": {"full_name": REPO},
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.test/qa",
        **changes,
    }


def compat_receipt(*, result: str = "passed", sha: str = SHA) -> dict:
    receipt = {
        "schema": gates.COMPAT_SCHEMA,
        "result": result,
        "source_sha": sha,
        "recorded_at": "2026-09-29T22:50:00Z",
        "subject": "server built from source_sha",
    }
    if result == "passed":
        receipt.update(
            previous_release_tag="v0.1.59",
            previous_engine={"asset": "longhouse-engine-linux-arm64", "engine_sha256": "c" * 64},
            counts={"tests": 6, "failures": 0, "errors": 0, "skipped": 0},
        )
    else:
        receipt["reason"] = "no published release precedes the candidate"
    return receipt


def archive_receipt(*, sha: str = SHA, digest: str = DIGEST, sealed: bool = True) -> dict:
    return {
        "schema": gates.ARCHIVE_SCHEMA,
        "component": "longhouse-runtime",
        "image_digest": digest.rsplit("@", 1)[-1],
        "source_sha": sha,
        "build_run_id": "555",
        "build_attempt": 1,
        "sealed": sealed,
        "manifest_key": "images/sha256/" + digest.rsplit(":", 1)[-1] + "/manifest.json",
        "blob_count": 9,
    }


def archive_run(**changes: Any) -> dict:
    """The run of main's Archive Runtime Image workflow that uploaded the archive receipt."""
    return {
        "id": ARCHIVE_RUN,
        "name": gates.ARCHIVE_WORKFLOW,
        "path": gates.ARCHIVE_WORKFLOW_PATH,
        "event": "workflow_run",
        "head_branch": "main",
        "head_sha": SHA,
        "head_repository": {"full_name": REPO},
        "repository": {"full_name": REPO},
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.test/archive",
        **changes,
    }


def artifact(artifact_id: int, name: str, receipt: dict, *, run: int, created: str, head_sha: str = SHA, expired: bool = False) -> dict:
    return {
        "id": artifact_id,
        "name": name,
        "expired": expired,
        "created_at": created,
        "workflow_run": {"id": run, "head_sha": head_sha},
        "receipt": receipt,
    }


def green_world() -> dict[str, Any]:
    return {
        "dogfood_health": {"status": "healthy", "build": {"commit": SHA, "dirty": False, "version": "0.1.61"}},
        "demo_health": {"status": "healthy", "build": {"commit": SHA, "dirty": False}},
        "deployments": [
            {
                "id": "d-dogfood",
                "status": "success",
                "source_sha": SHA,
                "image_digest": DIGEST,
                "submission_key": f"promote-dogfood-{DOGFOOD}-{SHA}",
                "completed_at": "2026-09-29T22:00:00.123456",
            }
        ],
        "deployment_detail": {
            "d-dogfood": {
                "id": "d-dogfood",
                "targets": [{"id": 1, "subdomain": DOGFOOD, "ring": 0, "deploy_state": "success"}],
                "target_evidence": [
                    {
                        "instance_id": 1,
                        "disposition": "success",
                        "instance_current_image": DIGEST,
                        "instance_applied_generation": 9,
                        "instance_desired_generation": 9,
                    }
                ],
            }
        },
        "soak": {
            "required_hours": 0,
            "enforced": False,
            "real_tenants": [],
            "pending_paid_intents": 0,
            "image": DIGEST,
            "satisfied": True,
            "soaked_by": None,
            "earliest_ready_at": None,
        },
        "instances": [
            {"id": 1, "subdomain": DOGFOOD, "status": "active"},
            {"id": 2, "subdomain": "erased", "status": "deprovisioned"},
        ],
        "artifacts": [
            artifact(101, f"hosted-live-qa-verdict-{SHA}", qa_receipt(), run=QA_RUN, created="2026-09-29T23:00:00Z"),
            artifact(102, f"engine-compat-{SHA}", compat_receipt(), run=COMPAT_RUN, created="2026-09-29T22:50:00Z"),
            artifact(104, f"runtime-oci-archive-{SHA}", archive_receipt(), run=ARCHIVE_RUN, created="2026-09-29T21:55:00Z"),
        ],
        "runs": {
            str(QA_RUN): qa_run(),
            str(ARCHIVE_RUN): archive_run(),
        },
        "publish_runs": [
            {"databaseId": 555, "number": 44, "attempt": 1, "headSha": SHA, "workflowName": "Publish Runtime Image", "conclusion": "success"}
        ],
    }


def add_real_tenant(world: dict[str, Any], *, satisfied: bool, soaked_by: dict | None = None, earliest: str | None = None) -> None:
    """A customer exists: the control plane turns the 24 h soak on."""
    world["soak"] = {
        **world["soak"],
        "required_hours": 24,
        "enforced": True,
        "real_tenants": [{"id": 7, "subdomain": "customer", "status": "active", "deploy_ring": 2}],
        "satisfied": satisfied,
        "soaked_by": soaked_by,
        "earliest_ready_at": earliest,
    }
    world["instances"].append({"id": 7, "subdomain": "customer", "status": "active"})


def zip_bytes(member: str, receipt: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, json.dumps(receipt))
    return buffer.getvalue()


def _member(name: str) -> str:
    if name.startswith("hosted-live-qa-verdict-"):
        return gates.QA_MEMBER
    if name.startswith("runtime-oci-archive-"):
        return gates.ARCHIVE_MEMBER
    return gates.COMPAT_MEMBER


class InProcessSources(gates.Sources):
    """The world, read without a network. A value of None for a key makes that read fail."""

    def __init__(self, world: dict[str, Any]) -> None:
        self.world = world

    def health(self, url: str) -> dict[str, Any]:
        body = self.world["dogfood_health"]
        if body is None:
            raise gates.SourceError("connection refused")
        return body

    def control(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        params = params or {}
        if path == "/api/deployments":
            rows = [row for row in self.world["deployments"] if row.get("submission_key") == params.get("submission_key")]
            return {"deployments": rows}
        if path == "/api/deployments/production-soak":
            if self.world["soak"] is None:
                raise gates.SourceError(f"{path} answered HTTP 404")
            return {**self.world["soak"], **({"image": params["image"]} if "image" in params and self.world["soak"].get("image") else {})}
        if path.startswith("/api/deployments/"):
            detail = self.world["deployment_detail"].get(path.rsplit("/", 1)[1])
            if detail is None:
                raise gates.SourceError(f"{path} answered HTTP 404")
            return detail
        if path == "/api/instances":
            if self.world["instances"] is None:
                raise gates.SourceError("/api/instances answered HTTP 500")
            return {"instances": self.world["instances"]}
        raise AssertionError(path)

    def github(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if path == f"repos/{REPO}/actions/artifacts":
            name = (params or {})["name"]
            return {
                "artifacts": [
                    {key: value for key, value in art.items() if key != "receipt"}
                    for art in self.world["artifacts"]
                    if art["name"] == name
                ]
            }
        if path.startswith(f"repos/{REPO}/actions/runs/"):
            run = self.world["runs"].get(path.rsplit("/", 1)[1])
            if run is None:
                raise gates.SourceError(f"gh api {path} failed: HTTP 404")
            return run
        raise AssertionError(path)

    def artifact_zip(self, repo: str, artifact_id: int) -> bytes:
        art = next(a for a in self.world["artifacts"] if a["id"] == artifact_id)
        return zip_bytes(_member(art["name"]), art["receipt"])


GH_STUB = f'''#!{sys.executable}
import json, os, sys, urllib.request
world = json.load(open(os.environ["FIXTURE_WORLD"]))
sys.path.insert(0, os.environ["FIXTURE_TESTS"])
import promotion_world as pw
args = sys.argv[1:]
if args[:2] == ["run", "list"]:
    print(json.dumps(world["publish_runs"]))
elif args[:1] == ["api"]:
    rest = args[1:]
    params = {{}}
    path = None
    i = 0
    while i < len(rest):
        if rest[i] == "-X":
            i += 2
        elif rest[i] == "-f":
            key, value = rest[i + 1].split("=", 1)
            params[key] = value
            i += 2
        else:
            path = rest[i]
            i += 1
    source = pw.InProcessSources(world)
    try:
        if path.endswith("/zip"):
            artifact_id = int(path.split("/")[-2])
            sys.stdout.buffer.write(source.artifact_zip(pw.REPO, artifact_id))
        else:
            print(json.dumps(source.github(path, params)))
    except pw.gates.SourceError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
else:
    raise AssertionError("gh must not be asked for: " + " ".join(args))
'''

SSH_STUB = f'''#!{sys.executable}
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
with open(root / "ssh_invocations", "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(1 if os.environ.get("FIXTURE_SSH_FAIL") == "1" else 0)
'''


class Wire:
    """The world over HTTP (control plane and health) with `gh` and `ssh` on PATH."""

    def __init__(self, world: dict[str, Any], root: Path) -> None:
        self.world = world
        self.root = root
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                if url.path == "/dogfood/api/health":
                    body = outer.world["dogfood_health"]
                    return self._send(200, body) if body is not None else self._send(503, {})
                if url.path == "/demo/api/health":
                    return self._send(200, outer.world["demo_health"])
                if self.headers.get("X-Admin-Token") != "fixture-token":
                    return self._send(403, {"detail": "Admin token required"})
                try:
                    if url.path == "/api/deployments/production-soak" and outer.world["soak"] is None:
                        return self._send(404, {"detail": "Deployment not found"})
                    return self._send(200, InProcessSources(outer.world).control(url.path, params))
                except gates.SourceError:
                    return self._send(404, {"detail": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "Wire":
        (self.root / "world.json").write_text(json.dumps(self.world))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, content in (("gh", GH_STUB), ("ssh", SSH_STUB)):
            path = bin_dir / name
            path.write_text(content)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "FIXTURE_ROOT": str(self.root),
            "FIXTURE_WORLD": str(self.root / "world.json"),
            "FIXTURE_TESTS": str(Path(__file__).resolve().parent),
            "FIXTURE_CONTROL_PLANE_URL": self.url,
            "SUBDOMAIN": DOGFOOD,
            "DOGFOOD_HEALTH_URL": f"{self.url}/dogfood/api/health",
            "DEMO_HEALTH_URL": f"{self.url}/demo/api/health",
            "DEMO_VERIFY_TIMEOUT": "1",
            "DEMO_POLL_SECONDS": "0.2",
            "PROMOTION_RECEIPT_DIR": str(self.root / "receipts"),
            "GITHUB_REPOSITORY": REPO,
        }


def clone(world: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(world)
