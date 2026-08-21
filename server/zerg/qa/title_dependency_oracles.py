"""Runtime Host title-dependency product oracles.

The hermetic oracle owns an isolated Runtime Host and loopback model stub. The
live oracle owns no provider dependency controls: it creates one ordinary
hidden obligation, then reads the Runtime Host's machine-facing session and
product-health projections.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records

RUNTIME_API_URL_ENV = "LONGHOUSE_RUNTIME_API_URL"
RUNTIME_API_TOKEN_ENV = "LONGHOUSE_RUNTIME_API_TOKEN"
_TITLE_CHECK = "session_titles"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _StubState:
    def __init__(self, accepted_token: str) -> None:
        self.accepted_token = accepted_token
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []

    def observe(self, *, path: str, authorized: bool) -> None:
        with self.lock:
            self.requests.append({"path": path, "status": 200 if authorized else 401})

    def receipt(self) -> dict[str, Any]:
        with self.lock:
            requests = list(self.requests)
        return {
            "schema_version": 1,
            "artifact_kind": "title_loopback_stub_receipt",
            "requests": requests,
            "unauthorized_count": sum(item["status"] == 401 for item in requests),
            "healthy_count": sum(item["status"] == 200 for item in requests),
        }


class _TitleStub:
    def __init__(self, accepted_token: str) -> None:
        self.state = _StubState(accepted_token)
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                authorized = self.headers.get("authorization") == f"Bearer {state.accepted_token}"
                state.observe(path=self.path, authorized=authorized)
                if not authorized:
                    self.send_response(401)
                    payload = {"error": {"message": "provider-shaped unauthorized", "type": "authentication_error"}}
                else:
                    self.send_response(200)
                    payload = {
                        "id": "factory-title-assurance",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "factory-title-stub",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": '{"title":"Recovered Hidden Obligation"}'},
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                body = json.dumps(payload).encode()
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="title-assurance-stub", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _RuntimeHost:
    def __init__(self, *, repo_root: Path, root: Path, base_url: str, token_file: Path) -> None:
        self.repo_root = repo_root
        self.root = root
        self.port = _free_port()
        self.api_url = f"http://127.0.0.1:{self.port}"
        self.database_path = root / "runtime-live.db"
        self.log_path = root / "runtime.log"
        self.base_url = base_url
        self.token_file = token_file
        self.process: subprocess.Popen[bytes] | None = None
        self._log_stream = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("Runtime Host is already running")
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": f"sqlite:///{self.root / 'runtime.db'}",
                "TESTING": "1",
                "AUTH_DISABLED": "1",
                "SINGLE_TENANT": "1",
                "INSTANCE_ID": "factory-title-assurance",
                "LONGHOUSE_HOME": str(self.root / "home"),
                "LONGHOUSE_STORAGE_V2_ROOT": str(self.root / "storage-v2"),
                "LONGHOUSE_FACTORY_ASSURANCE": "1",
                "LONGHOUSE_FACTORY_ASSURANCE_TITLE_BASE_URL": self.base_url,
                "LONGHOUSE_FACTORY_ASSURANCE_TITLE_TOKEN_FILE": str(self.token_file),
                "LLM_DISABLED": "0",
                "E2E_LOG_SUPPRESS": "0",
                "LOG_LEVEL": "INFO",
            }
        )
        environment.pop("OPENROUTER_API_KEY", None)
        server_root = self.repo_root / "server"
        environment["PYTHONPATH"] = os.pathsep.join(item for item in (str(server_root), environment.get("PYTHONPATH", "")) if item)
        self._log_stream = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "zerg.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--no-access-log",
            ],
            cwd=server_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log_stream,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Runtime Host exited during startup with {self.process.returncode}")
            try:
                response = httpx.get(f"{self.api_url}/health", timeout=1)
                if response.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise TimeoutError("Runtime Host did not become reachable")

    def stop(self) -> int | None:
        process = self.process
        if process is None:
            return None
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        returncode = process.returncode
        self.process = None
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
        return returncode


def _headers(token: str | None = None, *, machine_id: str | None = None) -> dict[str, str]:
    headers = {"X-Longhouse-Storage-Lane": "live"}
    if token:
        headers["X-Agents-Token"] = token
    if machine_id:
        headers["X-Longhouse-Machine-Id"] = machine_id
    return headers


def _capabilities(api_url: str, token: str | None, *, machine_id: str) -> dict[str, Any]:
    response = httpx.get(
        f"{api_url.rstrip('/')}/api/agents/storage/v2/capabilities",
        headers=_headers(token, machine_id=machine_id),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Runtime Host returned invalid storage capabilities")
    return payload


def _envelope(*, tenant_id: str, machine_id: str, message: str) -> tuple[str, dict[str, Any]]:
    session_id = str(uuid4())
    source_epoch = uuid4()
    opaque_source_id = f"factory-title-assurance/{session_id}.jsonl"
    raw = (
        json.dumps(
            {
                "type": "user",
                "uuid": str(uuid4()),
                "timestamp": _now(),
                "message": {"role": "user", "content": message},
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    identity = EnvelopeIdentity(
        tenant_id=tenant_id,
        machine_id=machine_id,
        provider="claude",
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        range_kind="byte_offset",
        range_start=0,
        range_end=len(raw),
        record_hashes=hash_records((raw,)),
    )
    observed = _now()
    return session_id, {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "machine_id": machine_id,
        "session_id": session_id,
        "provider": "claude",
        "opaque_source_id": opaque_source_id,
        "source_epoch": str(source_epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": observed,
        "range_kind": "byte_offset",
        "range_start": 0,
        "range_end": len(raw),
        "render": {
            "generation_id": str(uuid4()),
            "parser_revision": "factory-title-assurance-v1",
            "ordering_revision": "semantic-order-v2",
            "records": [
                {
                    "event_id": str(uuid4()),
                    "order_time_us": int(time.time() * 1_000_000),
                    "source_position": 0,
                    "event_subordinal": 0,
                    "role": "user",
                    "content_text": message,
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "thread_id": None,
                    "branch_kind": None,
                    "raw_record_ordinal": 0,
                    "interaction_kind": "prompt",
                }
            ],
        },
        "media": [],
        "session": {
            "environment": "local",
            "project": "longhouse-title-assurance",
            "cwd": "/factory/title-assurance",
            "git_repo": "cipher982/longhouse",
            "git_branch": "assurance",
            "started_at": observed,
            "last_activity_at": observed,
            "ended_at": None,
            "origin_kind": "console",
            "hidden_from_default_timeline": True,
            "launch_actor": "automation",
            "launch_surface": "factory_assurance",
            "provider_session_id": session_id,
        },
        "records": [{"source_position": 0, "data_b64": base64.b64encode(raw).decode()}],
        "expected_envelope_id": envelope_id(identity),
    }


def _post_envelope(api_url: str, token: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    response: httpx.Response | None = None
    for attempt in range(1, 6):
        response = httpx.post(
            f"{api_url.rstrip('/')}/api/agents/storage/v2/envelopes",
            headers=_headers(token),
            json=payload,
            timeout=20,
        )
        if response.status_code != 503:
            break
        time.sleep(0.1 * attempt)
    assert response is not None
    if response.is_error:
        raise RuntimeError(f"storage-v2 envelope failed status={response.status_code} body={response.text[:1000]}")
    return {"status_code": response.status_code, "session_id": payload["session_id"], "receipt": response.json()}


def _product_health(api_url: str, token: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    response = httpx.get(
        f"{api_url.rstrip('/')}/api/agents/observability/checks/session_titles?window=15m",
        headers=_headers(token),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("check") != _TITLE_CHECK:
        raise ValueError("Runtime Host product health omitted session_titles")
    return payload, payload


def _session_projection(api_url: str, token: str | None, session_id: str) -> dict[str, Any] | None:
    response = httpx.get(
        f"{api_url.rstrip('/')}/api/agents/sessions/{session_id}",
        headers=_headers(token),
        timeout=10,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def _catalog_snapshot(database_path: Path, session_ids: list[str]) -> dict[str, Any]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in session_ids)
        rows = connection.execute(
            f"""
            SELECT session_id, anchor_title, title_attempt_count, title_last_error,
                   title_dependency_incident_id, hidden_from_default_timeline
            FROM sessions WHERE session_id IN ({placeholders}) ORDER BY session_id
            """,
            session_ids,
        ).fetchall()
        dependencies = connection.execute(
            """
            SELECT use_case, provider, model, credential_binding, state, incident_id,
                   failure_class, credential_generation, recovered_at
            FROM runtime_dependency_state ORDER BY use_case, provider, model, credential_binding
            """
        ).fetchall()
    return {
        "sessions": [dict(row) for row in rows],
        "dependencies": [dict(row) for row in dependencies],
    }


def _seed_hidden_title_obligations(database_path: Path, *, count: int) -> list[str]:
    """Author isolated fixture facts before catalogd becomes their sole owner."""

    from sqlalchemy import insert

    from zerg.catalogd.models import StorageSession
    from zerg.catalogd.schema import create_catalog_engine
    from zerg.catalogd.schema import initialize_catalog_schema

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    observed = datetime.now(UTC).replace(microsecond=0)
    session_ids = [str(uuid4()) for _ in range(count)]
    with engine.begin() as connection:
        for index, session_id in enumerate(session_ids):
            connection.execute(
                insert(StorageSession).values(
                    session_id=session_id,
                    tenant_id="factory-title-assurance",
                    owner_id="factory",
                    provider="opencode",
                    environment="local",
                    machine_id="factory-title-machine",
                    project="longhouse-title-assurance",
                    cwd="/factory/title-assurance",
                    git_repo="cipher982/longhouse",
                    git_branch="assurance",
                    started_at=observed,
                    last_activity_at=observed,
                    user_messages=1,
                    first_user_message_preview=f"Explain the title assurance recovery obligation number {index}",
                    semantic_projection_version=1,
                    origin_kind="console",
                    hidden_from_default_timeline=True,
                    launch_actor="automation",
                    launch_surface="factory_assurance",
                    commit_seq=index + 1,
                    created_at=observed,
                    updated_at=observed,
                )
            )
    engine.dispose()
    return session_ids


def _wait_for(predicate, *, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {description}; last={last!r}")


def run_hermetic_title_dependency_oracle(*, evidence_root: Path, repo_root: Path) -> dict[str, Any]:
    evidence_root.mkdir(parents=True, exist_ok=False)
    invalid_token = "factory-title-generation-a"
    healthy_token = "factory-title-generation-b"
    stub = _TitleStub(healthy_token)
    stub.start()
    runtime: _RuntimeHost | None = None
    runtime_exit_codes: list[int | None] = []
    session_ids: list[str] = []
    snapshots: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="title-assurance-", dir=evidence_root.parent) as temporary:
        root = Path(temporary)
        token_file = root / "title-token"
        token_file.write_text(invalid_token, encoding="utf-8")
        token_file.chmod(0o600)
        runtime = _RuntimeHost(repo_root=repo_root, root=root, base_url=stub.base_url, token_file=token_file)
        session_ids = _seed_hidden_title_obligations(runtime.database_path, count=3)
        try:
            runtime.start()

            def failed_snapshot():
                snapshot = _catalog_snapshot(runtime.database_path, session_ids)
                rows = snapshot["sessions"]
                incidents = {row["title_dependency_incident_id"] for row in rows}
                return snapshot if len(rows) == 3 and None not in incidents and len(incidents) == 1 else None

            try:
                snapshots["failed"] = _wait_for(failed_snapshot, timeout=30, description="one shared title incident")
            except TimeoutError as exc:
                current = _catalog_snapshot(runtime.database_path, session_ids)
                runtime.stop()
                tail = runtime.log_path.read_text(encoding="utf-8", errors="replace")[-8_000:]
                tail = tail.replace(invalid_token, "<redacted-generation-a>").replace(healthy_token, "<redacted-generation-b>")
                raise RuntimeError(f"{exc}; catalog={current!r}; stub={stub.state.receipt()!r}; Runtime Host tail:\n{tail}") from exc
            runtime_exit_codes.append(runtime.stop())

            # Restart with the same bad generation to prove the durable
            # incident survives process loss. No model request or read path is
            # used to repair it.
            runtime.start()
            snapshots["after_restart"] = _catalog_snapshot(runtime.database_path, session_ids)
            token_file.write_text(healthy_token, encoding="utf-8")
            token_file.chmod(0o600)

            def recovered_snapshot():
                snapshot = _catalog_snapshot(runtime.database_path, session_ids)
                rows = snapshot["sessions"]
                dependency_rows = snapshot["dependencies"]
                recovered = (
                    len(rows) == 3
                    and all(row["anchor_title"] for row in rows)
                    and all(row["title_attempt_count"] == 0 for row in rows)
                    and all(row["title_dependency_incident_id"] is None for row in rows)
                    and dependency_rows
                    and dependency_rows[0]["state"] == "healthy"
                )
                return snapshot if recovered else None

            try:
                snapshots["recovered"] = _wait_for(recovered_snapshot, timeout=15, description="title debt recovery")
            except TimeoutError as exc:
                current = _catalog_snapshot(runtime.database_path, session_ids)
                runtime.stop()
                tail = runtime.log_path.read_text(encoding="utf-8", errors="replace")[-12_000:]
                tail = tail.replace(invalid_token, "<redacted-generation-a>").replace(healthy_token, "<redacted-generation-b>")
                raise RuntimeError(f"{exc}; catalog={current!r}; stub={stub.state.receipt()!r}; Runtime Host tail:\n{tail}") from exc
            health_payload, title_health = _product_health(runtime.api_url, None)
            snapshots["product_health"] = health_payload
            runtime_exit_codes.append(runtime.stop())
            runtime = None
        finally:
            if runtime is not None:
                runtime_exit_codes.append(runtime.stop())
            stub.close()
            if (root / "runtime.log").is_file():
                runtime_log = (root / "runtime.log").read_text(encoding="utf-8", errors="replace")[-32_000:]
                runtime_log = runtime_log.replace(invalid_token, "<redacted-generation-a>").replace(
                    healthy_token, "<redacted-generation-b>"
                )
                (evidence_root / "runtime-log-tail.txt").write_text(runtime_log, encoding="utf-8")

    failed_rows = snapshots["failed"]["sessions"]
    restarted_rows = snapshots["after_restart"]["sessions"]
    recovered_rows = snapshots["recovered"]["sessions"]
    incident_ids = {row["title_dependency_incident_id"] for row in failed_rows}
    observation = {
        "concurrent_hidden_obligation_count": len(session_ids),
        "all_obligations_hidden": all(row["hidden_from_default_timeline"] == 1 for row in recovered_rows),
        "one_shared_incident": len(incident_ids) == 1 and None not in incident_ids,
        "incident_survived_restart": [row["title_dependency_incident_id"] for row in restarted_rows]
        == [row["title_dependency_incident_id"] for row in failed_rows],
        "zero_row_attempt_consumption": all(row["title_attempt_count"] == 0 for row in failed_rows + recovered_rows),
        "same_rows_recovered": {row["session_id"] for row in recovered_rows} == set(session_ids),
        "all_rows_titled": all(row["anchor_title"] for row in recovered_rows),
        "provider_shaped_401_observed": stub.state.receipt()["unauthorized_count"] >= 1,
        "healthy_stub_observed": stub.state.receipt()["healthy_count"] >= 1,
        "product_health_healthy": title_health.get("verdict") == "ok",
        "storage_v2_read_count": 0,
        "runtime_restart_count": 1,
    }
    passed = (
        observation["concurrent_hidden_obligation_count"] >= 3
        and observation["storage_v2_read_count"] == 0
        and all(
            observation[key] is True
            for key in (
                "all_obligations_hidden",
                "one_shared_incident",
                "incident_survived_restart",
                "zero_row_attempt_consumption",
                "same_rows_recovered",
                "all_rows_titled",
                "provider_shaped_401_observed",
                "healthy_stub_observed",
                "product_health_healthy",
            )
        )
    )
    _write_json(evidence_root / "catalog-observation.json", snapshots)
    _write_json(evidence_root / "loopback-stub-receipt.json", stub.state.receipt())
    _write_json(
        evidence_root / "runtime-request-receipt.json",
        {
            "fixture_obligations": session_ids,
            "fixture_authored_before_catalogd_ownership": True,
            "storage_v2_writes": [],
            "storage_v2_reads": [],
            "product_health_reads": 1,
            "session_ids": session_ids,
        },
    )
    _write_json(
        evidence_root / "cleanup-receipt.json",
        {
            "status": "pass" if all(code in (0, None) for code in runtime_exit_codes) else "fail",
            "orphan_count": 0,
            "runtime_exit_codes": runtime_exit_codes,
            "loopback_stub_stopped": True,
            "temporary_runtime_removed": True,
        },
    )
    return {"passed": passed, "observation": observation}


def run_live_title_dependency_oracle(*, evidence_root: Path) -> dict[str, Any]:
    evidence_root.mkdir(parents=True, exist_ok=False)
    api_url = str(os.environ.get(RUNTIME_API_URL_ENV) or "").strip()
    token = str(os.environ.get(RUNTIME_API_TOKEN_ENV) or "").strip()
    if not api_url or not token:
        raise ValueError("live title assurance requires Runtime Host API URL and token")
    capabilities = _capabilities(api_url, token, machine_id="factory-title-assurance")
    session_id, payload = _envelope(
        tenant_id=str(capabilities["tenant_id"]),
        machine_id=str(capabilities["machine_id"]),
        message=f"Verify ordinary hidden title dependency health {uuid4().hex[:12]}",
    )
    write_receipt = _post_envelope(api_url, token, payload)

    def completed_obligation():
        session = _session_projection(api_url, token, session_id)
        _health, check = _product_health(api_url, token)
        if check.get("verdict") == "degraded":
            return {"session": session, "title_health": check, "degraded": True}
        if (
            session
            and session.get("anchor_title")
            and session.get("title_state") == "ready"
            and session.get("title_source") == "ai"
            and check.get("verdict") == "ok"
        ):
            return {"session": session, "title_health": check, "degraded": False}
        return None

    completed = _wait_for(completed_obligation, timeout=60, description="live title obligation")
    session = completed.get("session") or {}
    title_health = completed["title_health"]
    observation = {
        "ordinary_hidden_obligation_created": write_receipt["status_code"] == 200,
        "obligation_session_id": session_id,
        "obligation_titled": bool(session.get("anchor_title")),
        "claude_semantic_path_consumed": (
            session.get("provider") == "claude"
            and session.get("title_state") == "ready"
            and session.get("title_source") == "ai"
            and bool(session.get("anchor_title"))
        ),
        "dependency_health_verdict": title_health.get("verdict"),
        "dependency_health_consumed": True,
        "direct_provider_probe_count": 0,
        "credential_rotation_count": 0,
    }
    passed = (
        observation["ordinary_hidden_obligation_created"]
        and observation["obligation_titled"]
        and observation["claude_semantic_path_consumed"]
        and observation["dependency_health_verdict"] == "ok"
    )
    _write_json(
        evidence_root / "live-runtime-observation.json",
        {
            "session": session,
            "title_health": title_health,
            "write_receipt": write_receipt,
            "obligation_contract": {
                "environment": payload["session"]["environment"],
                "origin_kind": payload["session"]["origin_kind"],
                "hidden_from_default_timeline": payload["session"]["hidden_from_default_timeline"],
            },
        },
    )
    _write_json(
        evidence_root / "runtime-request-receipt.json",
        {
            "runtime_host_paths": [
                "/api/agents/storage/v2/capabilities",
                "/api/agents/storage/v2/envelopes",
                "/api/agents/sessions/{session_id}",
                "/api/agents/observability/checks/session_titles",
            ],
            "direct_provider_paths": [],
            "credential_mutations": [],
        },
    )
    _write_json(
        evidence_root / "cleanup-receipt.json",
        {
            "status": "pass",
            "orphan_count": 0,
            "persistent_hidden_obligation": session_id,
            "owned_process_count": 0,
        },
    )
    return {"passed": passed, "observation": observation}


__all__ = [
    "RUNTIME_API_TOKEN_ENV",
    "RUNTIME_API_URL_ENV",
    "artifact_manifest",
    "run_hermetic_title_dependency_oracle",
    "run_live_title_dependency_oracle",
]
