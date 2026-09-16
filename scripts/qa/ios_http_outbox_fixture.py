#!/usr/bin/env python3
"""Disposable Runtime Host-shaped HTTP fixture for the iOS outbox proof.

This is intentionally an external fixture, not an app test seam. It accepts the
same REST/SSE routes used by the normal authenticated client, drops the first
multipart acknowledgement after recording the request, and only serves the
receipt after the driver enables it on relaunch. The fixture records attachment
hashes and request identities so the XCTest can prove that no new operation was
allocated while the pending intent crossed a process restart.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


NOW = "2026-01-01T00:00:00.000Z"


class ProofState:
    def __init__(self, session_id: str, token: str, state_path: Path) -> None:
        self.session_id = session_id
        self.token = token
        self.state_path = state_path
        self.lock = threading.RLock()
        self.posts: list[dict[str, Any]] = []
        self.receipt_enabled = False
        self.receipt_requests = 0
        self.served_receipts = 0
        self.last_served_receipt: dict[str, Any] | None = None
        self.streams: list[dict[str, Any]] = []
        self.workspace_reads: list[str] = []
        self.persist()

    def persist(self) -> None:
        with self.lock:
            posts = [dict(post) for post in self.posts]
            state = {
                "session_id": self.session_id,
                "posts": posts,
                "receipt_enabled": self.receipt_enabled,
                "receipt_requests": self.receipt_requests,
                "served_receipts": self.served_receipts,
                "last_served_receipt": self.last_served_receipt,
                "streams": list(self.streams),
                "workspace_reads": list(self.workspace_reads),
                "attachment_bytes_equal": (
                    len(posts) >= 2
                    and posts[0]["attachment_sha256"] == posts[1]["attachment_sha256"]
                    and posts[0]["attachment_bytes"] == posts[1]["attachment_bytes"]
                ),
            }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")
            os.replace(temporary, self.state_path)

    def record_workspace_read(self) -> None:
        with self.lock:
            self.workspace_reads.append(
                self.streams[-1]["epoch"] if self.streams else "no-stream"
            )
            self.persist()


    def record_post(
        self,
        *,
        client_request_id: str,
        text: str,
        intent: str,
        filename: str,
        mime_type: str,
        attachment: bytes,
    ) -> int:
        with self.lock:
            post = {
                "client_request_id": client_request_id,
                "text": text,
                "intent": intent,
                "filename": filename,
                "mime_type": mime_type,
                "attachment_sha256": hashlib.sha256(attachment).hexdigest(),
                "attachment_bytes": len(attachment),
            }
            self.posts.append(post)
            index = len(self.posts)
            self.persist()
            return index

    def receipt(self) -> dict[str, Any] | None:
        with self.lock:
            if not self.receipt_enabled or not self.posts:
                return None
            post = self.posts[0]
            return {
                "client_request_id": post["client_request_id"],
                "intent": post["intent"],
                "status": "accepted",
                "event_id": "proof-event-1",
            }


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LonghouseIOSProof/1"

    @property
    def proof(self) -> ProofState:
        return self.server.proof  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        # Never put the bearer token or request bodies in the process log.
        print(f"[fixture] {self.command} {self.path} " + (fmt % args), flush=True)

    def authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        agents_token = self.headers.get("X-Agents-Token", "")
        return authorization == f"Bearer {self.proof.token}" or agents_token == self.proof.token

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json(401, {"detail": "fixture authentication required"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/__proof/state":
            if not self.require_auth():
                return
            try:
                state = json.loads(self.proof.state_path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                state = {}
            self.send_json(200, state)
            return
        if path == "/api/timeline/sessions":
            if not self.require_auth():
                return
            self.send_json(200, {"sessions": [timeline_card(self.proof.session_id)], "total": 1, "has_real_sessions": True})
            return
        match = re.fullmatch(r"/api/timeline/sessions/([^/]+)/workspace/stream", path)
        if match:
            if not self.require_auth():
                return
            if match.group(1) != self.proof.session_id:
                self.send_json(404, {"detail": "unknown session"})
                return
            self.stream()
            return
        match = re.fullmatch(r"/api/timeline/sessions/([^/]+)(?:/(workspace|mobile-tail|subagents))?", path)
        if match:
            if not self.require_auth():
                return
            if match.group(1) != self.proof.session_id:
                self.send_json(404, {"detail": "unknown session"})
                return
            suffix = match.group(2)
            if suffix == "subagents":
                self.send_json(200, {"subagents": []})
            elif suffix == "workspace":
                self.proof.record_workspace_read()
                self.send_json(200, workspace(self.proof.session_id, self.proof.receipt()))
            elif suffix == "mobile-tail":
                self.proof.record_workspace_read()
                self.send_json(200, mobile_tail(self.proof.session_id, self.proof.receipt()))
            else:
                self.proof.record_workspace_read()
                self.send_json(200, session_detail(self.proof.session_id, self.proof.receipt()))
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/inputs", path)
        if match:
            if not self.require_auth():
                return
            if match.group(1) != self.proof.session_id:
                self.send_json(404, {"detail": "unknown session"})
                return
            with self.proof.lock:
                self.proof.receipt_requests += 1
                self.proof.persist()
            receipt = self.proof.receipt()
            if receipt is not None:
                with self.proof.lock:
                    self.proof.served_receipts += 1
                    self.proof.last_served_receipt = dict(receipt)
                    self.proof.persist()
            self.send_json(200, {"inputs": [] if receipt is None else [receipt]})
            return
        self.send_json(404, {"detail": "fixture route not implemented"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/__proof/enable-receipt":
            if not self.require_auth():
                return
            self.proof.receipt_enabled = True
            self.proof.persist()
            self.send_json(200, {"receipt_enabled": True})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/inputs-multipart", path)
        if match:
            if not self.require_auth():
                return
            if match.group(1) != self.proof.session_id:
                self.send_json(404, {"detail": "unknown session"})
                return
            self.record_multipart()
            return
        # The proof must use the attachment route. A JSON input would not prove
        # that PhotosPicker bytes reached the production multipart client.
        if re.fullmatch(r"/api/sessions/([^/]+)/input", path):
            if self.require_auth():
                self.send_json(400, {"detail": "proof requires inputs-multipart"})
            return
        self.send_json(404, {"detail": "fixture route not implemented"})

    def record_multipart(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        boundary_header = self.headers.get("Content-Type", "")
        prefix = "boundary="
        if prefix not in boundary_header:
            self.send_json(400, {"detail": "missing multipart boundary"})
            return
        boundary = boundary_header.split(prefix, 1)[1].strip().strip('"')
        fields = parse_multipart(body, boundary.encode("utf-8"))
        text = fields.get("text", ("", b""))[0]
        intent = fields.get("intent", ("", b""))[0]
        client_request_id = fields.get("client_request_id", ("", b""))[0]
        filename, mime_type, attachment = fields.get("attachments", ("", b"application/octet-stream", b""))
        if not client_request_id or not attachment:
            self.send_json(400, {"detail": "missing proof input fields"})
            return
        post_number = self.proof.record_post(
            client_request_id=client_request_id,
            text=text,
            intent=intent,
            filename=filename,
            mime_type=mime_type,
            attachment=attachment,
        )
        if post_number == 1:
            # Deliberately no HTTP response: URLSession sees an ambiguous
            # transport result after this durable fixture-side record.
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        self.send_json(
            200,
            {
                "outcome": "sent",
                "input_id": 7,
                "live_input_id": "proof-live-input-7",
                "client_request_id": client_request_id,
                "intent": intent,
                "queued": [],
            },
        )

    def stream(self) -> None:
        with self.proof.lock:
            stream_number = len(self.proof.streams) + 1
            epoch = f"proof-epoch-{stream_number}"
            self.proof.streams.append({"number": stream_number, "epoch": epoch})
            self.proof.persist()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        connected = {
            "session_id": self.proof.session_id,
            "server_now_ms": int(time.time() * 1000),
            "stream_epoch": epoch,
        }
        changed = {
            "session_id": self.proof.session_id,
            "latest_event_id": 1,
            "change_kind": "runtime",
            "thread_session_count": 1,
            "latest_event_emitted_at_ms": int(time.time() * 1000),
            "server_fanout_at_ms": int(time.time() * 1000),
            "server_now_ms": int(time.time() * 1000),
            "catalog_commit_seq": 1,
            "pubsub_seq": stream_number,
            "stream_epoch": epoch,
            "transcript_preview": None,
        }
        frames = [
            f"event: connected\ndata: {json.dumps(connected, separators=(',', ':'))}\n\n",
            f"id: {stream_number}\nevent: workspace_changed\ndata: {json.dumps(changed, separators=(',', ':'))}\n\n",
            ": proof-heartbeat\n\n",
        ]
        try:
            for frame in frames:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
            # Keep the connection alive long enough for the driver to terminate
            # and relaunch. Client termination closes this handler's socket.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                time.sleep(0.5)
                self.wfile.write(b": proof-heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, tuple[Any, ...]]:
    marker = b"--" + boundary
    fields: dict[str, tuple[Any, ...]] = {}
    for part in body.split(marker)[1:]:
        part = part.strip(b"\r\n-")
        if not part or b"\r\n\r\n" not in part:
            continue
        header_bytes, value = part.split(b"\r\n\r\n", 1)
        headers = {}
        for raw in header_bytes.split(b"\r\n"):
            if b":" in raw:
                key, raw_value = raw.split(b":", 1)
                headers[key.decode("latin1").strip().lower()] = raw_value.decode("latin1").strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        if filename_match:
            fields[name] = (filename_match.group(1), headers.get("content-type", "application/octet-stream"), value)
        else:
            fields[name] = (value.decode("utf-8", "replace"), value)
    return fields


def base_session(session_id: str, receipt: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": session_id,
        "origin_kind": "runtime_host",
        "provider": "claude",
        "provider_session_id": "proof-provider-session",
        "project": "ios-http-proof",
        "device_id": "proof-device",
        "environment": "test",
        "cwd": "/tmp/ios-http-proof",
        "git_repo": None,
        "git_branch": None,
        "started_at": NOW,
        "ended_at": None,
        "user_messages": 1,
        "assistant_messages": 1,
        "tool_calls": 0,
        "last_activity_at": NOW,
        "timeline_anchor_at": NOW,
        "runtime_phase": "idle",
        "phase_started_at": NOW,
        "last_progress_at": NOW,
        "runtime_source": "proof",
        "terminal_state": None,
        "runtime_version": 1,
        "status": "working",
        "presence_state": "idle",
        "presence_tool": None,
        "presence_updated_at": NOW,
        "last_live_at": NOW,
        "display_phase": "Idle",
        "active_tool": None,
        "confidence": "high",
        "summary": "HTTP outbox proof session",
        "summary_title": "HTTP outbox proof",
        "anchor_title": "HTTP outbox proof",
        "timeline_title": "HTTP outbox proof",
        "title_state": "ready",
        "title_source": "fixture",
        "hidden_from_default_timeline": False,
        "launch_actor": "proof",
        "launch_surface": "test",
        "summary_status": "ready",
        "first_user_message": "",
        "match_event_id": None,
        "match_snippet": None,
        "match_role": None,
        "match_score": None,
        "thread_root_session_id": session_id,
        "thread_head_session_id": session_id,
        "thread_continuation_count": 0,
        "continued_from_session_id": None,
        "continuation_kind": None,
        "origin_label": "HTTP fixture",
        "home_label": "Proof",
        "branched_from_event_id": None,
        "is_writable_head": True,
        "is_sidechain": False,
        "control": None,
        "capabilities": {
            "live_control_available": True,
            "host_reattach_available": False,
            "reply_to_live_session_available": True,
            "can_queue_next_input": True,
            "can_steer_active_turn": False,
            "display_label": "HTTP fixture",
            "display_detail": "Disposable loopback Runtime Host",
            "display_tone": "success",
            "input_mode": "composer",
            "default_input_intent": "auto",
            "composer_enabled": True,
            "composer_placeholder": "Send a proof message",
            "composer_disabled_reason": None,
            "send_disabled_reason": None,
            "turn_state": "idle",
            "can_start_turn": False,
            "start_turn_blocked_by": None,
            "can_interrupt_active_turn": False,
            "attach_images": True,
        },
        "session_state": session_state_facts(),
        "runtime_display": runtime_display(),
        "transcript_preview": {
            "event_id": 1,
            "text": "HTTP fixture ready.",
            "role": "assistant",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "tool_call_id": None,
            "tool_call_state": None,
            "event_origin": "durable",
            "timestamp": NOW,
            "is_provisional": False,
            "is_complete": True,
            "content_cursor": None,
            "is_stale": False,
            "stale_reason": None,
        },
        "input_receipts": [] if receipt is None else [receipt],
        "last_turn": None,
        "recap": None,
        "usage_latest": None,
        "timeline_card": {
            "ownership": {"label": "Proof", "tone": "success"},
            "status": {"label": "Ready", "tone": "success", "seen_at": NOW, "seen_at_prefix": ""},
            "border_tone": "active",
        },
        "user_state": "active",
        "user_hidden_from_timeline": False,
        "execution_lifetime": "proof",
        "sharer": None,
    }


def session_state_facts() -> dict[str, Any]:
    available = {"state": "available", "reason": None}
    unavailable = {"state": "unavailable", "reason": "proof"}
    return {
        "state_contract_version": 1,
        "presentation_policy_version": 1,
        "mode": "managed",
        "disposition": {"state": "open", "closed_at": None, "close_reason": None},
        "launch": {"state": "ready", "error_code": None, "error_message": None},
        "run": {"id": "proof-run", "lifecycle": "active", "started_at": NOW, "ended_at": None, "end_reason": None},
        "activity": {"state": "idle", "raw_kind": None, "tool": None, "source": "proof", "observed_at": NOW, "valid_until": NOW},
        "control": {
            "ownership": "owned",
            "connection": "connected",
            "connection_id": "proof-connection",
            "lease_generation": "proof-lease",
            "control_plane": "proof",
            "terminal_attached": True,
            "observed_at": NOW,
            "valid_until": NOW,
            "actions": {
                "start_turn": unavailable,
                "send_input": available,
                "interrupt": unavailable,
                "terminate": unavailable,
                "reattach": unavailable,
                "resume": unavailable,
                "branch": unavailable,
            },
        },
        "pending_interaction": None,
        "transcript": {"convergence": "durable", "source_revision": 1, "durable_revision": 1, "render_revision": 1, "last_append_at": NOW, "searchable": True, "live_observation": True},
        "host": {"state": "online", "observed_at": NOW},
        "working_set": "proof",
        "unread": False,
        "last_result_at": NOW,
        "last_result_outcome": "success",
        "presentation": {"primary": None, "access": None, "transcript": None},
        "commit_seq": 1,
    }


def runtime_display() -> dict[str, Any]:
    return {
        "truth_tier": "fresh",
        "signal_tier": "process_binding",
        "state": "idle",
        "tone": "active",
        "headline": "HTTP fixture ready",
        "detail": "Disposable loopback Runtime Host",
        "phase_label": "Idle",
        "compact_tool_label": None,
        "is_live": True,
        "is_executing": False,
        "needs_attention": False,
        "is_idle": True,
        "is_stalled": False,
        "is_managed_local_truth": False,
        "has_signal": True,
        "control_path": "managed",
        "activity_recency": "live",
        "lifecycle": "open",
        "host_state": "online",
        "terminal_reason": None,
        "pause_request": None,
    }


def event_item(session_id: str) -> dict[str, Any]:
    return {
        "kind": "event",
        "session_id": session_id,
        "timestamp": NOW,
        "event": {
            "id": 1,
            "role": "assistant",
            "content_text": "HTTP fixture ready.",
            "interaction_kind": None,
            "raw_content_text": None,
            "input_origin": None,
            "turn_end": None,
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "tool_output_truncated": None,
            "tool_output_original_chars": None,
            "tool_call_id": None,
            "tool_presentation": None,
            "timestamp": NOW,
            "in_active_context": True,
            "branch_id": None,
            "is_head_branch": True,
            "event_origin": "durable",
            "provisional_state": None,
            "provisional_cursor": None,
            "provisional_complete": None,
            "reconciled_event_id": None,
            "tool_call_state": None,
            "media_refs": [],
        },
        "action": None,
        "continued_from_session_id": None,
        "continuation_kind": None,
        "origin_label": "HTTP fixture",
        "parent_origin_label": None,
        "parent_continuation_kind": None,
        "branched_from_event_id": None,
    }


def workspace(session_id: str, receipt: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "session": base_session(session_id, receipt),
        "thread": {"root_session_id": session_id, "head_session_id": session_id, "sessions": [base_session(session_id, receipt)]},
        "projection": {
            "root_session_id": session_id,
            "focus_session_id": session_id,
            "head_session_id": session_id,
            "path_session_ids": [session_id],
            "items": [event_item(session_id)],
            "total": 1,
            "page_offset": 0,
            "branch_mode": "head",
            "abandoned_events": 0,
            "generation_id": "proof-generation",
            "next_cursor": None,
            "has_more": False,
        },
        "workspace_revision": {
            "latest_event_id": 1,
            "latest_session_updated_at": NOW,
            "latest_runtime_signal_at": NOW,
            "runtime_version_sum": 1,
            "pause_request_count": 0,
            "pause_request_fingerprint": "",
            "managed_control_count": 1,
            "managed_control_fingerprint": "proof-control",
            "live_preview_updated_at": NOW,
            "thread_session_count": 1,
            "fingerprint": "proof-fingerprint",
        },
        "control_only": False,
    }


def mobile_tail(session_id: str, receipt: dict[str, Any] | None) -> dict[str, Any]:
    value = workspace(session_id, receipt)
    return {
        "session": value["session"],
        "projection": value["projection"],
        "snapshot_event_id": 1,
        "workspace_revision": value["workspace_revision"],
    }


def session_detail(session_id: str, receipt: dict[str, Any] | None) -> dict[str, Any]:
    return base_session(session_id, receipt)


def timeline_card(session_id: str) -> dict[str, Any]:
    session = base_session(session_id, None)
    return {
        "thread_id": session_id,
        "timeline_anchor_at": NOW,
        "head": session,
        "detail": session,
        "root": session,
        "continuation_count": 0,
        "started_origin_label": "HTTP fixture",
        "head_origin_label": "HTTP fixture",
    }


class ProofHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], proof: ProofState) -> None:
        super().__init__(address, FixtureHandler)
        self.proof = proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    proof = ProofState(args.session_id, args.auth_token, args.state)
    server = ProofHTTPServer(("127.0.0.1", args.port), proof)
    host, port = server.server_address
    args.ready.parent.mkdir(parents=True, exist_ok=True)
    args.ready.write_text(
        json.dumps(
            {
                "server_url": f"http://{host}:{port}",
                "state_url": f"http://{host}:{port}/__proof/state",
                "enable_receipt_url": f"http://{host}:{port}/__proof/enable-receipt",
                "session_id": args.session_id,
            },
            sort_keys=True,
        )
        + "\n"
    )
    print(f"[fixture] ready http://{host}:{port} session={args.session_id}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
