"""Put a transcript into a served Runtime Host and read its events back over HTTP.

A verifier that judges how the product presents tool calls must not import the
presentation code. It posts rendered events to the host the way the Machine
Agent does (a storage-v2 envelope) and reads what the host serves. The envelope
identity is computed with the shared ``provider_contract`` vocabulary, so a
change to the product's wire contract fails the check instead of moving with it.
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import time
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.provider_contract.catalogd_process import CatalogdHandle
from zerg.provider_contract.storage_envelope_contract import EnvelopeIdentity
from zerg.provider_contract.storage_envelope_contract import envelope_id
from zerg.provider_contract.storage_envelope_contract import hash_records
from zerg.qa.provider_artifacts import now
from zerg.qa.repo_root import default_repo_root
from zerg.qa.runtime_host import RuntimeHost

_MACHINE_ID = "served-transcript-verifier"
_LANE_HEADERS = {"X-Longhouse-Storage-Lane": "live"}


@contextmanager
def transcript_host() -> Iterator[RuntimeHost]:
    """A served Runtime Host that owns its catalog and has its one owner, stopped and removed on exit."""

    # A Unix socket path is limited to 104 bytes, so the host lives under /tmp.
    root = Path(tempfile.mkdtemp(prefix="lh-transcript-", dir="/tmp"))
    host = RuntimeHost(repo_root=default_repo_root(), root=root, instance_id="served-transcript", extra_env={"ENVIRONMENT": "test:e2e"})
    try:
        host.start()
        catalog = CatalogdHandle(database_path=host.database_path, socket_path=host.catalogd_socket())
        # Auth is off, so the host serves as its one active owner; the catalog starts without one.
        catalog.execute("INSERT INTO users (id, email, is_active) VALUES (?, ?, 1)", (1, "served-transcript@longhouse.invalid"))
        yield host
    finally:
        host.stop()
        shutil.rmtree(root, ignore_errors=True)


def _record_role(event: Mapping[str, Any]) -> str:
    return str(event.get("role") or "assistant")


def ingest_events(
    api_url: str,
    *,
    provider: str,
    events: Sequence[Mapping[str, Any]],
    session_id: str | None = None,
) -> str:
    """Post ``events`` (role, content_text, tool_name, tool_input_json, tool_output_text, tool_call_id) as one session.

    Returns the session id. Events keep their order. The raw record behind each
    rendered event is its own canonical JSON line.
    """

    api_url = api_url.rstrip("/")
    headers = {**_LANE_HEADERS, "X-Longhouse-Machine-Id": _MACHINE_ID}
    capabilities = httpx.get(f"{api_url}/api/agents/storage/v2/capabilities", headers=headers, timeout=15)
    capabilities.raise_for_status()
    tenant_id = str(capabilities.json()["tenant_id"])

    session_id = session_id or str(uuid4())
    source_epoch = uuid4()
    opaque_source_id = f"served-transcript/{session_id}.jsonl"
    raw_lines = tuple(json.dumps(dict(event), sort_keys=True, separators=(",", ":")).encode() + b"\n" for event in events)
    offsets: list[int] = []
    total = 0
    for line in raw_lines:
        offsets.append(total)
        total += len(line)
    identity = EnvelopeIdentity(
        tenant_id=tenant_id,
        machine_id=_MACHINE_ID,
        provider=provider,
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        range_kind="byte_offset",
        range_start=0,
        range_end=total,
        record_hashes=hash_records(raw_lines),
    )
    observed = now()
    base_us = int(time.time() * 1_000_000)
    render_records = [
        {
            "event_id": str(uuid4()),
            "order_time_us": base_us + index,
            "source_position": offsets[index],
            "event_subordinal": 0,
            "role": _record_role(event),
            "content_text": event.get("content_text"),
            "tool_name": event.get("tool_name"),
            "tool_input_json": event.get("tool_input_json"),
            "tool_output_text": event.get("tool_output_text"),
            "tool_call_id": event.get("tool_call_id"),
            "thread_id": None,
            "branch_kind": None,
            "raw_record_ordinal": index,
            "interaction_kind": "prompt" if _record_role(event) == "user" else None,
        }
        for index, event in enumerate(events)
    ]
    payload = {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "machine_id": _MACHINE_ID,
        "session_id": session_id,
        "provider": provider,
        "opaque_source_id": opaque_source_id,
        "source_epoch": str(source_epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": observed,
        "range_kind": "byte_offset",
        "range_start": 0,
        "range_end": total,
        "render": {
            "generation_id": str(uuid4()),
            "parser_revision": "served-transcript-v1",
            "ordering_revision": "semantic-order-v2",
            "records": render_records,
        },
        "media": [],
        "session": {
            "environment": "local",
            "project": "served-transcript",
            "cwd": "/served-transcript",
            "git_repo": None,
            "git_branch": None,
            "started_at": observed,
            "last_activity_at": observed,
            "ended_at": None,
            "origin_kind": "test_or_canary",
            "hidden_from_default_timeline": True,
            "launch_actor": "automation",
            "launch_surface": "test",
            "provider_session_id": session_id,
        },
        "records": [
            {"source_position": offsets[index], "data_b64": base64.b64encode(line).decode()} for index, line in enumerate(raw_lines)
        ],
        "expected_envelope_id": envelope_id(identity),
    }
    response: httpx.Response | None = None
    for attempt in range(1, 6):
        response = httpx.post(f"{api_url}/api/agents/storage/v2/envelopes", headers=_LANE_HEADERS, json=payload, timeout=30)
        if response.status_code != 503:
            break
        time.sleep(0.1 * attempt)
    assert response is not None
    if response.is_error:
        raise RuntimeError(f"storage-v2 envelope failed status={response.status_code} body={response.text[:1000]}")
    return session_id


def read_events(api_url: str, session_id: str, *, timeout_seconds: float = 30.0) -> list[dict[str, Any]]:
    """The events the host serves for one session, in order, once they are readable."""

    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{api_url.rstrip('/')}/api/agents/sessions/{session_id}/events",
            params={"limit": 500},
            headers=_LANE_HEADERS,
            timeout=30,
        )
        if response.status_code == 200:
            events = response.json().get("events")
            if isinstance(events, list) and events:
                return [dict(event) for event in events]
        last = f"{response.status_code} {response.text[:300]}"
        time.sleep(0.2)
    raise TimeoutError(f"Runtime Host served no events for {session_id}: {last}")
