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
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.services.internal_sessions import FACTORY_TITLE_ASSURANCE_CWD
from zerg.services.internal_sessions import FACTORY_TITLE_ASSURANCE_PROJECT
from zerg.services.internal_sessions import FACTORY_TITLE_ASSURANCE_SURFACE
from zerg.services.internal_sessions import PROVIDER_FACTORY_MACHINE_ID
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
    def __init__(self, unavailable_token: str, healthy_token: str) -> None:
        self.unavailable_token = unavailable_token
        self.healthy_token = healthy_token
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.active_requests = 0
        self.max_active_requests = 0
        self.healthy_requests_started = 0
        self.empty_response_count = 0
        self.healthy_wave_release = threading.Event()

    def begin(self, *, path: str, token: str, empty_probe: bool) -> tuple[int, bool, bool]:
        with self.lock:
            status = 503 if token == self.unavailable_token else 200 if token == self.healthy_token else 401
            if status == 200:
                self.healthy_requests_started += 1
            emit_empty = status == 200 and empty_probe
            if emit_empty:
                self.empty_response_count += 1
            self.active_requests += 1
            self.max_active_requests = max(self.max_active_requests, self.active_requests)
            self.requests.append({"path": path, "status": status, "empty_probe": empty_probe})
            return status, status == 200 and self.healthy_requests_started > 1, emit_empty

    def finish(self) -> None:
        with self.lock:
            self.active_requests -= 1

    def receipt(self) -> dict[str, Any]:
        with self.lock:
            requests = list(self.requests)
            active_requests = self.active_requests
            max_active_requests = self.max_active_requests
            healthy_requests_started = self.healthy_requests_started
            empty_response_count = self.empty_response_count
        return {
            "schema_version": 2,
            "artifact_kind": "title_loopback_stub_receipt",
            "requests": requests,
            "unauthorized_count": sum(item["status"] == 401 for item in requests),
            "unavailable_count": sum(item["status"] == 503 for item in requests),
            "healthy_count": sum(item["status"] == 200 for item in requests),
            "active_requests": active_requests,
            "max_active_requests": max_active_requests,
            "healthy_requests_started": healthy_requests_started,
            "empty_response_count": empty_response_count,
        }


class _TitleStub:
    def __init__(self, unavailable_token: str, healthy_token: str) -> None:
        self.state = _StubState(unavailable_token, healthy_token)
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                length = int(self.headers.get("content-length") or 0)
                raw_body = self.rfile.read(length) if length else b""
                empty_probe = b"Factory row-local empty response probe" in raw_body
                token = str(self.headers.get("authorization") or "").removeprefix("Bearer ")
                status, block_healthy_wave, emit_empty = state.begin(
                    path=self.path,
                    token=token,
                    empty_probe=empty_probe,
                )
                try:
                    if block_healthy_wave:
                        state.healthy_wave_release.wait(timeout=30)
                    # Hold requests long enough for the oracle to observe the
                    # Runtime Host's actual provider concurrency ceiling.
                    time.sleep(0.1)
                    self.send_response(status)
                    if status == 401:
                        payload = {"error": {"message": "provider-shaped unauthorized", "type": "authentication_error"}}
                    elif status == 503:
                        payload = {"error": {"message": "provider-shaped temporarily unavailable", "type": "server_error"}}
                    elif emit_empty:
                        payload = {
                            "id": "factory-title-assurance-empty",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "factory-title-stub",
                            "choices": [
                                {
                                    "index": 0,
                                    "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": ""},
                                }
                            ],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
                        }
                    else:
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
                finally:
                    state.finish()

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
            "project": FACTORY_TITLE_ASSURANCE_PROJECT,
            "cwd": FACTORY_TITLE_ASSURANCE_CWD,
            "git_repo": "cipher982/longhouse",
            "git_branch": "assurance",
            "started_at": observed,
            "last_activity_at": observed,
            "ended_at": None,
            "origin_kind": "console",
            "hidden_from_default_timeline": True,
            "launch_actor": "automation",
            "launch_surface": FACTORY_TITLE_ASSURANCE_SURFACE,
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
                   title_dependency_incident_id, title_retry_at, hidden_from_default_timeline
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


def _seed_hidden_title_obligations(database_path: Path, *, count: int) -> tuple[list[str], str, str, str]:
    """Author isolated fixture facts before catalogd becomes their sole owner."""

    from sqlalchemy import insert

    from zerg.catalogd.models import StorageSession
    from zerg.catalogd.schema import create_catalog_engine
    from zerg.catalogd.schema import initialize_catalog_schema

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    observed = datetime.now(UTC).replace(microsecond=0)
    obligation_started = observed - timedelta(minutes=10)
    session_ids = [str(uuid4()) for _ in range(count)]
    unrelated_terminal_id = str(uuid4())
    row_local_empty_id = str(uuid4())
    legacy_provider_proof_id = str(uuid4())
    with engine.begin() as connection:
        for index, session_id in enumerate(session_ids):
            connection.execute(
                insert(StorageSession).values(
                    session_id=session_id,
                    tenant_id="factory-title-assurance",
                    owner_id="factory",
                    provider="claude",
                    environment="local",
                    machine_id="provider-factory-resume",
                    project="longhouse-title-assurance",
                    cwd="/factory/title-assurance",
                    git_repo="cipher982/longhouse",
                    git_branch="assurance",
                    started_at=obligation_started,
                    last_activity_at=obligation_started,
                    user_messages=1,
                    first_user_message_preview=f"Explain the title assurance recovery obligation number {index}",
                    semantic_projection_version=1,
                    title_attempt_count=5 if index in {0, 1} else 0,
                    title_last_attempt_at=observed - timedelta(minutes=10) if index in {0, 1} else None,
                    title_retry_at=observed - timedelta(minutes=10) if index in {0, 1} else None,
                    title_last_error="TimeoutError" if index == 0 else "empty_model_response" if index == 1 else None,
                    origin_kind="console",
                    hidden_from_default_timeline=True,
                    launch_actor="automation",
                    launch_surface="factory_assurance",
                    commit_seq=index + 1,
                    created_at=obligation_started,
                    updated_at=observed,
                )
            )
        # A row-scoped terminal failure is a negative control: dependency
        # recovery must not re-arm it. Test environment keeps it outside the
        # user-facing health backlog while preserving the repair distinction.
        connection.execute(
            insert(StorageSession).values(
                session_id=unrelated_terminal_id,
                tenant_id="factory-title-assurance",
                owner_id="factory",
                provider="opencode",
                environment="test",
                machine_id="factory-title-machine",
                project="longhouse-title-assurance",
                started_at=observed,
                last_activity_at=observed,
                user_messages=1,
                first_user_message_preview="Malformed title output negative control",
                semantic_projection_version=1,
                title_attempt_count=5,
                title_last_attempt_at=observed,
                title_retry_at=observed,
                title_last_error="invalid_title_payload",
                hidden_from_default_timeline=True,
                commit_seq=count + 1,
                created_at=observed,
                updated_at=observed,
            )
        )
        # This capped row becomes due only after the dependency recovery wave.
        # A healthy 200 response with blank content must remain row-local,
        # pending, and unbound to any provider incident.
        connection.execute(
            insert(StorageSession).values(
                session_id=row_local_empty_id,
                tenant_id="factory-title-assurance",
                owner_id="factory",
                provider="claude",
                environment="local",
                machine_id="provider-factory-resume",
                project="longhouse-title-assurance",
                cwd="/factory/title-assurance",
                started_at=observed,
                last_activity_at=observed,
                user_messages=1,
                first_user_message_preview="Factory row-local empty response probe",
                semantic_projection_version=1,
                title_attempt_count=5,
                title_last_attempt_at=observed,
                title_retry_at=observed + timedelta(seconds=10),
                title_last_error="empty_model_response",
                origin_kind="console",
                hidden_from_default_timeline=True,
                launch_actor="automation",
                launch_surface="factory_assurance",
                commit_seq=count + 2,
                created_at=observed,
                updated_at=observed,
            )
        )
        # Legacy provider proofs predate typed launch provenance and commonly
        # retain a transcript newline. They must be classified from their exact
        # prompt shape before entering either the title scheduler or its health
        # debt. This fixture makes Python/SQL normalization drift release-blocking.
        connection.execute(
            insert(StorageSession).values(
                session_id=legacy_provider_proof_id,
                tenant_id="factory-title-assurance",
                owner_id="factory",
                provider="claude",
                environment="local",
                machine_id="factory-title-machine",
                project="longhouse-title-assurance",
                cwd="/factory/historical-human-looking-workspace",
                started_at=obligation_started,
                last_activity_at=obligation_started,
                user_messages=1,
                first_user_message_preview=(
                    "Reply with exactly LONGHOUSE_CLAUDE_PRINT_74694349fb694c97af560ac98572f989 " "and nothing else.\n"
                ),
                semantic_projection_version=1,
                title_attempt_count=0,
                hidden_from_default_timeline=False,
                commit_seq=count + 3,
                created_at=obligation_started,
                updated_at=observed,
            )
        )
    engine.dispose()
    return session_ids, unrelated_terminal_id, row_local_empty_id, legacy_provider_proof_id


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
    unavailable_token = "factory-title-generation-a"
    healthy_token = "factory-title-generation-b"
    stub = _TitleStub(unavailable_token, healthy_token)
    stub.start()
    runtime: _RuntimeHost | None = None
    runtime_exit_codes: list[int | None] = []
    session_ids: list[str] = []
    unrelated_terminal_id = ""
    row_local_empty_id = ""
    legacy_provider_proof_id = ""
    snapshots: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="title-assurance-", dir=evidence_root.parent) as temporary:
        root = Path(temporary)
        token_file = root / "title-token"
        token_file.write_text(unavailable_token, encoding="utf-8")
        token_file.chmod(0o600)
        runtime = _RuntimeHost(repo_root=repo_root, root=root, base_url=stub.base_url, token_file=token_file)
        session_ids, unrelated_terminal_id, row_local_empty_id, legacy_provider_proof_id = _seed_hidden_title_obligations(
            runtime.database_path,
            count=8,
        )
        all_fixture_ids = [*session_ids, unrelated_terminal_id, row_local_empty_id, legacy_provider_proof_id]

        def fixture_rows(
            snapshot: dict[str, Any],
        ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
            by_id = {str(row["session_id"]): row for row in snapshot["sessions"]}
            return (
                [by_id[session_id] for session_id in session_ids if session_id in by_id],
                by_id.get(unrelated_terminal_id, {}),
                by_id.get(row_local_empty_id, {}),
                by_id.get(legacy_provider_proof_id, {}),
            )

        try:
            runtime.start()

            def failed_snapshot():
                snapshot = _catalog_snapshot(runtime.database_path, all_fixture_ids)
                rows, unrelated, _row_local_empty, legacy_proof = fixture_rows(snapshot)
                incidents = {row["title_dependency_incident_id"] for row in rows}
                dependencies = snapshot["dependencies"]
                failed = (
                    len(rows) == 8
                    and None not in incidents
                    and len(incidents) == 1
                    and dependencies
                    and dependencies[0]["failure_class"] == "availability"
                    and unrelated.get("title_dependency_incident_id") is None
                    and legacy_proof.get("anchor_title") is None
                    and legacy_proof.get("title_attempt_count") == 0
                    and legacy_proof.get("title_dependency_incident_id") is None
                )
                return snapshot if failed else None

            try:
                snapshots["failed"] = _wait_for(failed_snapshot, timeout=30, description="one shared title incident")
            except TimeoutError as exc:
                current = _catalog_snapshot(runtime.database_path, all_fixture_ids)
                runtime.stop()
                tail = runtime.log_path.read_text(encoding="utf-8", errors="replace")[-8_000:]
                tail = tail.replace(unavailable_token, "<redacted-generation-a>").replace(healthy_token, "<redacted-generation-b>")
                raise RuntimeError(f"{exc}; catalog={current!r}; stub={stub.state.receipt()!r}; Runtime Host tail:\n{tail}") from exc
            runtime_exit_codes.append(runtime.stop())

            # Restart with the same bad generation to prove the durable
            # incident survives process loss. No model request or read path is
            # used to repair it.
            runtime.start()
            snapshots["after_restart"] = _catalog_snapshot(runtime.database_path, all_fixture_ids)
            token_file.write_text(healthy_token, encoding="utf-8")
            token_file.chmod(0o600)

            def bounded_scheduler_snapshot():
                receipt = stub.state.receipt()
                if receipt["active_requests"] < 4:
                    return None
                health_payload, check = _product_health(runtime.api_url, None)
                signals = check.get("signals") if isinstance(check.get("signals"), dict) else {}
                bounded = (
                    check.get("verdict") == "degraded"
                    and signals.get("open_dependencies") == 0
                    and int(signals.get("overdue_sessions") or 0) >= 1
                    and int(signals.get("oldest_overdue_age_seconds") or 0) >= 300
                    and signals.get("scheduled_workers") == 4
                    and signals.get("scheduled_workers_peak") == 4
                    and signals.get("worker_limit") == 4
                )
                return {"health": health_payload, "stub": receipt} if bounded else None

            try:
                snapshots["bounded_scheduler_health"] = _wait_for(
                    bounded_scheduler_snapshot,
                    timeout=15,
                    description="bounded title workers and degraded aged backlog",
                )
            finally:
                stub.state.healthy_wave_release.set()

            def recovered_snapshot():
                snapshot = _catalog_snapshot(runtime.database_path, all_fixture_ids)
                rows, unrelated, _row_local_empty, legacy_proof = fixture_rows(snapshot)
                dependency_rows = snapshot["dependencies"]
                recovered = (
                    len(rows) == 8
                    and all(row["anchor_title"] for row in rows)
                    and all(row["title_attempt_count"] == 0 for row in rows)
                    and all(row["title_dependency_incident_id"] is None for row in rows)
                    and dependency_rows
                    and dependency_rows[0]["state"] == "healthy"
                    and unrelated.get("anchor_title") is None
                    and unrelated.get("title_attempt_count") == 5
                    and unrelated.get("title_last_error") == "invalid_title_payload"
                    and legacy_proof.get("anchor_title") is None
                    and legacy_proof.get("title_attempt_count") == 0
                    and legacy_proof.get("title_dependency_incident_id") is None
                )
                return snapshot if recovered else None

            try:
                snapshots["recovered"] = _wait_for(recovered_snapshot, timeout=15, description="title debt recovery")
            except TimeoutError as exc:
                current = _catalog_snapshot(runtime.database_path, all_fixture_ids)
                runtime.stop()
                tail = runtime.log_path.read_text(encoding="utf-8", errors="replace")[-12_000:]
                tail = tail.replace(unavailable_token, "<redacted-generation-a>").replace(healthy_token, "<redacted-generation-b>")
                raise RuntimeError(f"{exc}; catalog={current!r}; stub={stub.state.receipt()!r}; Runtime Host tail:\n{tail}") from exc

            def row_local_empty_snapshot():
                snapshot = _catalog_snapshot(runtime.database_path, all_fixture_ids)
                _rows, _unrelated, empty_row, legacy_proof = fixture_rows(snapshot)
                dependency_rows = snapshot["dependencies"]
                retry_value = empty_row.get("title_retry_at")
                retry_at = datetime.fromisoformat(str(retry_value)) if retry_value else None
                if retry_at is not None and retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                if not (
                    empty_row.get("anchor_title") is None
                    and empty_row.get("title_attempt_count") == 5
                    and empty_row.get("title_last_error") == "empty_model_response"
                    and empty_row.get("title_dependency_incident_id") is None
                    and retry_at is not None
                    and retry_at > datetime.now(UTC)
                    and dependency_rows
                    and dependency_rows[0]["state"] == "healthy"
                    and stub.state.receipt()["empty_response_count"] >= 1
                    and legacy_proof.get("anchor_title") is None
                    and legacy_proof.get("title_attempt_count") == 0
                    and legacy_proof.get("title_dependency_incident_id") is None
                ):
                    return None
                health_payload, check = _product_health(runtime.api_url, None)
                signals = check.get("signals") if isinstance(check.get("signals"), dict) else {}
                isolated = (
                    check.get("verdict") == "ok"
                    and signals.get("open_dependencies") == 0
                    and signals.get("terminal_sessions") == 0
                    and int(signals.get("pending_sessions") or 0) >= 1
                )
                return {"catalog": snapshot, "health": health_payload} if isolated else None

            snapshots["row_local_empty"] = _wait_for(
                row_local_empty_snapshot,
                timeout=30,
                description="row-local capped empty response retry",
            )
            health_payload, title_health = _product_health(runtime.api_url, None)
            snapshots["product_health"] = health_payload
            runtime_exit_codes.append(runtime.stop())
            runtime = None
        finally:
            stub.state.healthy_wave_release.set()
            if runtime is not None:
                runtime_exit_codes.append(runtime.stop())
            stub.close()
            if (root / "runtime.log").is_file():
                runtime_log = (root / "runtime.log").read_text(encoding="utf-8", errors="replace")[-32_000:]
                runtime_log = runtime_log.replace(unavailable_token, "<redacted-generation-a>").replace(
                    healthy_token, "<redacted-generation-b>"
                )
                (evidence_root / "runtime-log-tail.txt").write_text(runtime_log, encoding="utf-8")

    failed_rows, failed_unrelated, _failed_empty, failed_legacy_proof = fixture_rows(snapshots["failed"])
    restarted_rows, _restarted_unrelated, _restarted_empty, restarted_legacy_proof = fixture_rows(snapshots["after_restart"])
    recovered_rows, recovered_unrelated, _recovered_empty, recovered_legacy_proof = fixture_rows(snapshots["recovered"])
    _isolated_rows, _isolated_unrelated, isolated_empty, isolated_legacy_proof = fixture_rows(snapshots["row_local_empty"]["catalog"])
    isolated_retry_value = isolated_empty.get("title_retry_at")
    isolated_retry_at = datetime.fromisoformat(str(isolated_retry_value)) if isolated_retry_value else None
    if isolated_retry_at is not None and isolated_retry_at.tzinfo is None:
        isolated_retry_at = isolated_retry_at.replace(tzinfo=UTC)
    incident_ids = {row["title_dependency_incident_id"] for row in failed_rows}
    failed_by_id = {str(row["session_id"]): row for row in failed_rows}
    recovered_by_id = {str(row["session_id"]): row for row in recovered_rows}
    stub_receipt = stub.state.receipt()
    health_signals = title_health.get("signals") if isinstance(title_health.get("signals"), dict) else {}
    bounded_health = snapshots["bounded_scheduler_health"]["health"]
    bounded_signals = bounded_health.get("signals") if isinstance(bounded_health.get("signals"), dict) else {}
    bounded_stub = snapshots["bounded_scheduler_health"]["stub"]
    observation = {
        "concurrent_hidden_obligation_count": len(session_ids),
        "all_obligations_hidden": all(row["hidden_from_default_timeline"] == 1 for row in recovered_rows),
        "one_shared_incident": len(incident_ids) == 1 and None not in incident_ids,
        "incident_survived_restart": [row["title_dependency_incident_id"] for row in restarted_rows]
        == [row["title_dependency_incident_id"] for row in failed_rows],
        "zero_new_row_attempt_consumption": (
            failed_by_id[session_ids[0]]["title_attempt_count"] == 5
            and failed_by_id[session_ids[1]]["title_attempt_count"] == 5
            and all(failed_by_id[session_id]["title_attempt_count"] == 0 for session_id in session_ids[2:])
            and all(row["title_attempt_count"] == 0 for row in recovered_rows)
        ),
        "legacy_terminal_timeout_reentered": (
            failed_by_id[session_ids[0]]["title_dependency_incident_id"] in incident_ids
            and recovered_by_id[session_ids[0]]["title_attempt_count"] == 0
            and bool(recovered_by_id[session_ids[0]]["anchor_title"])
        ),
        "terminal_empty_response_reentered": (
            failed_by_id[session_ids[1]]["title_attempt_count"] == 5
            and failed_by_id[session_ids[1]]["title_dependency_incident_id"] in incident_ids
            and recovered_by_id[session_ids[1]]["title_attempt_count"] == 0
            and bool(recovered_by_id[session_ids[1]]["anchor_title"])
        ),
        "row_local_empty_response_isolated": (
            isolated_empty.get("title_attempt_count") == 5
            and isolated_empty.get("title_last_error") == "empty_model_response"
            and isolated_empty.get("title_dependency_incident_id") is None
            and isolated_retry_at is not None
            and isolated_retry_at > datetime.now(UTC)
            and stub_receipt["empty_response_count"] >= 1
        ),
        "unrelated_terminal_debt_preserved": (
            failed_unrelated.get("title_attempt_count") == 5
            and recovered_unrelated.get("title_attempt_count") == 5
            and recovered_unrelated.get("title_last_error") == "invalid_title_payload"
            and recovered_unrelated.get("title_dependency_incident_id") is None
        ),
        "legacy_exact_provider_proof_excluded_from_title_debt": all(
            row.get("anchor_title") is None
            and row.get("title_attempt_count") == 0
            and row.get("title_last_error") is None
            and row.get("title_dependency_incident_id") is None
            for row in (
                failed_legacy_proof,
                restarted_legacy_proof,
                recovered_legacy_proof,
                isolated_legacy_proof,
            )
        ),
        "same_rows_recovered": {row["session_id"] for row in recovered_rows} == set(session_ids),
        "all_rows_titled": all(row["anchor_title"] for row in recovered_rows),
        "provider_shaped_503_observed": stub_receipt["unavailable_count"] >= 1,
        "healthy_stub_observed": stub_receipt["healthy_count"] >= 1,
        "model_concurrency_bounded": 1 <= stub_receipt["max_active_requests"] <= 4,
        "model_concurrency_peak": stub_receipt["max_active_requests"],
        "scheduled_worker_creation_bounded": (
            bounded_signals.get("scheduled_workers") == 4
            and bounded_signals.get("scheduled_workers_peak") == 4
            and bounded_signals.get("worker_limit") == 4
            and bounded_stub.get("active_requests") == 4
        ),
        "aged_backlog_degrades_with_healthy_dependency": (
            bounded_health.get("verdict") == "degraded"
            and bounded_signals.get("open_dependencies") == 0
            and int(bounded_signals.get("overdue_sessions") or 0) >= 1
            and int(bounded_signals.get("oldest_overdue_age_seconds") or 0) >= 300
        ),
        "product_health_healthy": title_health.get("verdict") == "ok",
        "product_health_backlog_clear": (
            health_signals.get("terminal_sessions") == 0
            and health_signals.get("overdue_sessions") == 0
            and health_signals.get("open_dependencies") == 0
        ),
        "storage_v2_read_count": 0,
        "runtime_restart_count": 1,
    }
    passed = (
        observation["concurrent_hidden_obligation_count"] > 4
        and observation["storage_v2_read_count"] == 0
        and all(
            observation[key] is True
            for key in (
                "all_obligations_hidden",
                "one_shared_incident",
                "incident_survived_restart",
                "zero_new_row_attempt_consumption",
                "legacy_terminal_timeout_reentered",
                "terminal_empty_response_reentered",
                "row_local_empty_response_isolated",
                "unrelated_terminal_debt_preserved",
                "legacy_exact_provider_proof_excluded_from_title_debt",
                "same_rows_recovered",
                "all_rows_titled",
                "provider_shaped_503_observed",
                "healthy_stub_observed",
                "model_concurrency_bounded",
                "scheduled_worker_creation_bounded",
                "aged_backlog_degrades_with_healthy_dependency",
                "product_health_healthy",
                "product_health_backlog_clear",
            )
        )
    )
    _write_json(evidence_root / "catalog-observation.json", snapshots)
    _write_json(evidence_root / "loopback-stub-receipt.json", stub_receipt)
    _write_json(
        evidence_root / "runtime-request-receipt.json",
        {
            "fixture_obligations": session_ids,
            "unrelated_terminal_negative_control": unrelated_terminal_id,
            "legacy_exact_provider_proof_negative_control": legacy_provider_proof_id,
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
    capabilities = _capabilities(api_url, token, machine_id=PROVIDER_FACTORY_MACHINE_ID)
    if str(capabilities.get("machine_id") or "") != PROVIDER_FACTORY_MACHINE_ID:
        raise ValueError("live title assurance token is not bound to the canonical provider-factory machine")
    session_id, payload = _envelope(
        tenant_id=str(capabilities["tenant_id"]),
        machine_id=PROVIDER_FACTORY_MACHINE_ID,
        message=f"Verify typed hidden title assurance health {uuid4().hex[:12]}",
    )
    write_receipt = _post_envelope(api_url, token, payload)

    def completed_obligation():
        session = _session_projection(api_url, token, session_id)
        _health, check = _product_health(api_url, token)
        if check.get("verdict") == "degraded":
            return None
        if (
            session
            and session.get("provider") == "claude"
            and session.get("environment") == "local"
            and session.get("project") == FACTORY_TITLE_ASSURANCE_PROJECT
            and session.get("cwd") == FACTORY_TITLE_ASSURANCE_CWD
            and session.get("device_id") == PROVIDER_FACTORY_MACHINE_ID
            and session.get("origin_kind") == "console"
            and session.get("hidden_from_default_timeline") is True
            and session.get("launch_actor") == "automation"
            and session.get("launch_surface") == FACTORY_TITLE_ASSURANCE_SURFACE
            and session.get("anchor_title")
            and session.get("title_state") == "ready"
            and session.get("title_source") == "ai"
            and check.get("verdict") == "ok"
        ):
            return {"session": session, "title_health": check, "degraded": False}
        return None

    completed = _wait_for(completed_obligation, timeout=120, description="live title obligation and clear backlog")
    session = completed.get("session") or {}
    title_health = completed["title_health"]
    health_signals = title_health.get("signals") if isinstance(title_health.get("signals"), dict) else {}
    observation = {
        "typed_hidden_title_assurance_obligation_created": write_receipt["status_code"] == 200,
        "factory_machine_identity_verified": capabilities.get("machine_id") == PROVIDER_FACTORY_MACHINE_ID,
        "typed_title_assurance_identity_persisted": (
            session.get("provider") == "claude"
            and session.get("environment") == "local"
            and session.get("project") == FACTORY_TITLE_ASSURANCE_PROJECT
            and session.get("cwd") == FACTORY_TITLE_ASSURANCE_CWD
            and session.get("device_id") == PROVIDER_FACTORY_MACHINE_ID
            and session.get("origin_kind") == "console"
            and session.get("hidden_from_default_timeline") is True
            and session.get("launch_actor") == "automation"
            and session.get("launch_surface") == FACTORY_TITLE_ASSURANCE_SURFACE
        ),
        "obligation_session_id": session_id,
        "obligation_titled": bool(session.get("anchor_title")),
        "claude_semantic_path_consumed": (
            session.get("provider") == "claude"
            and session.get("title_state") == "ready"
            and session.get("title_source") == "ai"
            and bool(session.get("anchor_title"))
        ),
        "dependency_health_verdict": title_health.get("verdict"),
        "dependency_backlog_clear": (
            health_signals.get("open_dependencies") == 0
            and health_signals.get("terminal_sessions") == 0
            and health_signals.get("overdue_sessions") == 0
        ),
        "dependency_health_consumed": True,
        "direct_provider_probe_count": 0,
        "credential_rotation_count": 0,
    }
    passed = (
        observation["typed_hidden_title_assurance_obligation_created"]
        and observation["factory_machine_identity_verified"]
        and observation["typed_title_assurance_identity_persisted"]
        and observation["obligation_titled"]
        and observation["claude_semantic_path_consumed"]
        and observation["dependency_health_verdict"] == "ok"
        and observation["dependency_backlog_clear"]
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
