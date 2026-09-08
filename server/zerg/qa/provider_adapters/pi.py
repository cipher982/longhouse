"""Pi (earendil-works/pi) adapter for the universal provider harness.

Pi is a standalone TypeScript/Bun coding-agent CLI (npm
@earendil-works/pi-coding-agent, binary ``pi``). One-shot ``pi -p`` turns are
the first real surface: Longhouse launches the stock CLI against a live model,
reads Pi's append-only session JSONL, and ingests the parsed transcript into a
Longhouse database.

The session JSONL lives under ``--session-dir`` (default ``~/.pi/agent/sessions/<cwd-encoded>/``).
Its schema:

* A single ``{"type":"session","version":3,"id":<uuidv7>,"timestamp","cwd"}`` header line.
* Append-only tree entries, each with ``id``/``parentId``/``timestamp``/``type``:
  ``message`` (with an AgentMessage ``message`` field whose ``content`` is a
  list of text/tool blocks), ``model_change``, ``thinking_level_change``,
  ``compaction``, ``branch_summary``, and others.

The adapter exercises the stock tool-enabled print path and keeps the exact
native session file for continuation; no provider-neutral transcript is used
as a substitute for Pi's JSONL history.

Live-spending discipline: the real ``pi -p`` path only runs when both an
OpenRouter key is present AND ``LONGHOUSE_PI_LIVE=1`` is set, so a developer
laptop or CI with a key exported never silently spends tokens. Without live
opt-in (or against generated fake binaries) the adapter delegates to the base
session-safe projection or reports an honest gap.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.qa.provider_build_store import GENERATED_FAKE_PROVENANCE
from zerg.qa.universal_agent_harness import STATUS_FAIL
from zerg.qa.universal_agent_harness import STATUS_PASS
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import project_canonical_events_for_harness
from zerg.qa.universal_agent_harness import register_adapter

# Pi's built-in provider id plus the qualification model. The model is
# overridable through the env so CI can pin a concrete one without editing the
# adapter; the default is a floating -latest alias, so it is cd-only.
PI_PROVIDER = "openrouter"
PI_MODEL_ENV = "LONGHOUSE_PI_QUALIFICATION_MODEL"
PI_DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
PI_LIVE_ENV = "LONGHOUSE_PI_LIVE"
PI_RUN_TIMEOUT_SECS = 120
PI_INTERRUPT_WAIT_SECS = 20
PI_EVIDENCE_TEXT_LIMIT = 2000

# These are the provider-native JSONL shapes used by the Shadow projector. The
# taxonomy is intentionally keyed by native entry/content shape, not by the
# provider-neutral rows emitted below.
PI_SHADOW_TAXONOMY = {
    "session": "state:session_header",
    "message/user/text": "transcript:user",
    "message/user/image+text": "transcript:user_with_image",
    "message/assistant/text": "transcript:assistant",
    "message/assistant/text+thinking+toolCall": "transcript:assistant_tool",
    "message/toolResult": "provider_tool:result",
    "message/toolResult/image": "provider_tool:result_image",
    "model_change": "state:model",
    "thinking_level_change": "state:thinking_level",
    "compaction": "signal:context.compaction",
    "branch_summary": "state:branch",
    "custom": "extension:state",
    "custom_message": "extension:message",
    "session_info": "state:session_info",
}


def pi_qualification_model() -> str:
    return os.environ.get(PI_MODEL_ENV) or PI_DEFAULT_MODEL


def _scrub(text: str, secret: str | None) -> str:
    """Redact a known secret from evidence text (never leak the key)."""
    if not text:
        return text
    if secret:
        text = text.replace(secret, "***REDACTED***")
    return text


def _trunc(text: str, limit: int = PI_EVIDENCE_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _pi_text_content(message: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Return text, tool calls, thinking blocks, and image metadata."""
    texts: list[str] = []
    tools: list[dict[str, Any]] = []
    thinking: list[str] = []
    images: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str):
        return content, tools, thinking, images
    if not isinstance(content, list):
        return "\n".join(texts), tools, thinking, images
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind == "thinking" and block.get("thinking"):
            thinking.append(str(block["thinking"]))
        elif kind == "image":
            images.append({key: block.get(key) for key in ("mimeType", "mime_type") if block.get(key)})
        elif kind in {"toolCall", "tool_call", "tool"}:
            tools.append(dict(block))
    return "\n".join(texts), tools, thinking, images


def _pi_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block["text"]) for block in content if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    )


def _pi_native_shape(entry: Mapping[str, Any]) -> str:
    if entry.get("type") != "message":
        return str(entry.get("type") or "unknown")
    message = entry.get("message")
    if not isinstance(message, Mapping):
        return "message/unknown"
    role = str(message.get("role") or "unknown").strip()
    content = message.get("content")
    block_types: set[str] = set()
    if isinstance(content, list):
        block_types = {str(block.get("type") or "unknown") for block in content if isinstance(block, Mapping)}
    elif isinstance(content, str):
        block_types.add("string")
    suffix = "+".join(sorted(block_types)) or "unknown"
    if role == "toolResult" and "image" in block_types:
        return "message/toolResult/image"
    return f"message/{role}/{suffix}"


def pi_native_shadow_taxonomy(
    rows: list[Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify native Pi evidence without manufacturing provider rows."""
    shapes = {str(key): int(value) for key, value in dict(metadata.get("native_shapes") or {}).items()}
    classes: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for shape, count in shapes.items():
        classification = PI_SHADOW_TAXONOMY.get(shape)
        if classification is None:
            unmapped[shape] = count
        else:
            classes[classification] = classes.get(classification, 0) + count
    calls = {str(row.get("tool_call_id")) for row in rows if row.get("type") == "assistant" and row.get("tool_call_id")}
    results = {str(row.get("tool_call_id")) for row in rows if row.get("type") == "tool_result" and row.get("tool_call_id")}
    return {
        "source": "pi_native_session_jsonl",
        "native_shapes": shapes,
        "shadow_classes": classes,
        "unmapped_shapes": unmapped,
        "tool_call_ids": sorted(calls),
        "tool_result_ids": sorted(results),
        "tool_pairs": sorted(calls & results),
        "tool_calls_without_results": sorted(calls - results),
        "tool_results_without_calls": sorted(results - calls),
        "header_present": bool(metadata.get("has_header")),
        "provider_session_id": metadata.get("provider_session_id"),
    }


def _pi_session_header(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            header = json.loads(stream.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return header if isinstance(header, dict) and header.get("type") == "session" else None


def session_file_for_id(session_dir: Path, session_id: str) -> Path | None:
    """Resolve one exact native session header; never select by mtime."""
    matches = [
        path.resolve()
        for path in session_dir.rglob("*.jsonl")
        if path.is_file() and not path.is_symlink() and (_pi_session_header(path) or {}).get("id") == session_id
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Pi native session id is ambiguous: {session_id}")
    return matches[0] if matches else None


def pi_transcript_rows(transcript: Path) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Parse a Pi session JSONL into Longhouse raw-event rows.

    Returns ``(rows, provider_session_id, metadata)`` where metadata carries the
    first model_change, the jsonl line count, and whether a session header was
    seen (required for a valid transcript binding).
    """
    rows: list[dict[str, Any]] = []
    header_id: str | None = None
    metadata: dict[str, Any] = {
        "lines": 0,
        "model": None,
        "has_header": False,
        "taxonomy": {},
        "cwd": None,
        "version": None,
        "native_shapes": {},
        "provider_session_id": None,
    }
    try:
        lines = transcript.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        return rows, None, {**metadata, "error": f"{type(exc).__name__}: {exc}"}

    source_offset = 0
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace")
        if not line.strip():
            source_offset += len(raw_line)
            continue
        metadata["lines"] += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            source_offset += len(raw_line)
            continue
        if not isinstance(entry, dict):
            source_offset += len(raw_line)
            continue
        kind = str(entry.get("type") or "")
        metadata["taxonomy"][kind] = int(metadata["taxonomy"].get(kind, 0)) + 1
        native_shape = _pi_native_shape(entry)
        metadata["native_shapes"][native_shape] = int(metadata["native_shapes"].get(native_shape, 0)) + 1
        base = {
            "provider_event_type": kind,
            "entry_id": entry.get("id"),
            "parent_id": entry.get("parentId"),
            "timestamp": entry.get("timestamp"),
            "source_offset": source_offset,
            "source_line_sha256": hashlib.sha256(raw_line).hexdigest(),
        }
        if kind == "session":
            header_id = str(entry.get("id") or "") or header_id
            if header_id:
                metadata["has_header"] = True
                metadata["version"] = entry.get("version")
                metadata["cwd"] = entry.get("cwd")
                metadata["provider_session_id"] = header_id
        elif kind == "message":
            message = entry.get("message")
            if not isinstance(message, dict):
                source_offset += len(raw_line)
                continue
            role = str(message.get("role") or "").strip().lower()
            text, tools, thinking, images = _pi_text_content(message)
            row: dict[str, Any] = {
                **base,
                "role": role,
                "text": text,
                "message": {
                    "provider_role": role,
                    "stop_reason": message.get("stopReason"),
                    "error_message": message.get("errorMessage"),
                    "usage": message.get("usage"),
                    "provider": message.get("provider"),
                    "model": message.get("model"),
                },
            }
            if role == "user":
                row["type"] = "user"
            elif role == "assistant":
                row["type"] = "assistant"
            elif role == "toolresult":
                row["type"] = "tool_result"
                row["tool_call_id"] = message.get("toolCallId")
                row["tool_name"] = message.get("toolName")
                row["is_error"] = message.get("isError")
                row["text"] = _pi_content_text(message.get("content"))
            else:
                row["type"] = "provider_message"
            if tools:
                row["tool_calls"] = tools
                row["tool_name"] = tools[0].get("name")
                row["tool_call_id"] = tools[0].get("id")
                row["tool_input_json"] = tools[0].get("arguments")
            if thinking:
                row["thinking"] = thinking
            if images:
                row["images"] = images
            if header_id:
                row["provider_session_id"] = header_id
            rows.append(row)
        elif kind == "model_change" and metadata.get("model") is None:
            metadata["model"] = entry.get("modelId")
            rows.append({**base, "type": "model_change", "model": entry.get("modelId"), "provider": entry.get("provider")})
        elif kind in {
            "thinking_level_change",
            "compaction",
            "branch_summary",
            "custom",
            "custom_message",
            "session_info",
            "label",
        }:
            rows.append(
                {
                    **base,
                    "type": kind,
                    "text": entry.get("summary") or entry.get("content") or entry.get("name"),
                    "details": {key: value for key, value in entry.items() if key not in {"type", "id", "parentId", "timestamp"}},
                }
            )
        elif kind:
            rows.append({**base, "type": "provider_event", "details": dict(entry)})
        source_offset += len(raw_line)
    return rows, header_id, metadata


@register_adapter("pi")
class PiHarnessAdapter(UniversalProviderAdapter):
    """Pi concrete adapter for the universal Longhouse action contract."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session_file: Path | None = None
        self._provider_session_id: str | None = None

    def _resolve_untyped_binary(self, package: EvidencePackage, scenario: str) -> tuple[Path | None, dict[str, Any] | None]:
        probe = self.probe(package)
        if probe.get("status") != STATUS_PASS:
            return None, {
                **probe,
                "status": STATUS_FAIL,
                "failure_code": probe.get("failure_code") or f"{scenario}_probe_failed",
            }
        binary = self.provider_bin or Path(probe.get("path") or probe.get("declared_binary_name") or "pi")
        return binary, None

    def _pi_environment(self) -> dict[str, str]:
        """Minimal allowlisted env. Never forward the full os.environ: a provider
        auth failure can dump headers/config that carry secrets into stderr."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "PI_OFFLINE": os.environ.get("PI_OFFLINE", ""),
        }
        key = os.environ.get("OPENROUTER_API_KEY")
        if key:
            env["OPENROUTER_API_KEY"] = key
        return env

    @staticmethod
    def _has_credential() -> bool:
        return bool((os.environ.get("OPENROUTER_API_KEY") or "").strip())

    @staticmethod
    def _live_opted_in() -> bool:
        return os.environ.get(PI_LIVE_ENV) in {"1", "true", "yes", "on"}

    def _use_live(self) -> bool:
        """True only when this run should spend a real pi model turn.

        Requires an explicit live opt-in AND a credential, and is never true for
        generated fake binaries. Without it the adapter never spends tokens.
        """
        if self.provider_build is not None and self.provider_build.artifact_provenance == GENERATED_FAKE_PROVENANCE:
            return False
        return self._has_credential() and self._live_opted_in()

    def _run_pi_turn(
        self,
        package: EvidencePackage,
        prompt: str,
        marker: str,
        *,
        resume_file: Path | None = None,
    ) -> dict[str, Any]:
        binary, error = self._resolve_untyped_binary(package, "pi_turn")
        if error is not None:
            return error
        session_dir = package.path("pi", "sessions")
        session_dir.mkdir(parents=True, exist_ok=True)
        workdir = package.path("workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        expected_session_id = self._provider_session_id if resume_file is not None else str(uuid.uuid4())
        prior_size = 0
        if resume_file is not None:
            try:
                prior_size = resume_file.stat().st_size
            except OSError:
                prior_size = 0
        command = [
            str(binary),
            "-p",
            prompt,
            "--provider",
            PI_PROVIDER,
            "--model",
            pi_qualification_model(),
            "--session-dir",
            str(session_dir),
        ]
        if resume_file is None:
            command.extend(("--session-id", expected_session_id))
        else:
            resume_file = resume_file.resolve()
            if not resume_file.is_file() or _pi_session_header(resume_file) is None:
                return {
                    "status": STATUS_FAIL,
                    "failure_code": "pi_resume_file_invalid",
                    "message": "Pi exact resume requires an existing native session file.",
                    "session_file": str(resume_file),
                }
            command.extend(("--session", str(resume_file)))
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=str(workdir),
                env=self._pi_environment(),
                text=True,
                capture_output=True,
                check=False,
                timeout=PI_RUN_TIMEOUT_SECS,
            )
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(
                command,
                returncode=124,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            )
            timed_out = True
        elapsed = round(time.monotonic() - started, 3)
        secret = os.environ.get("OPENROUTER_API_KEY")
        stdout = _scrub(result.stdout or "", secret)
        stderr = _scrub(result.stderr or "", secret)
        package.write_text("pi/print.stdout.txt", stdout)
        package.write_text("pi/print.stderr.txt", stderr)
        transcript = resume_file if resume_file is not None else session_file_for_id(session_dir, expected_session_id)
        transcript_path = str(transcript) if transcript else None
        if transcript is None:
            return {
                "status": STATUS_FAIL,
                "failure_code": "pi_transcript_missing",
                "message": "real pi run completed without writing a session JSONL",
                "argv": command,
                "returncode": result.returncode,
                "timed_out": timed_out,
                "elapsed_secs": elapsed,
                "stdout": _trunc(stdout),
                "stderr": _trunc(stderr),
                "session_dir": str(session_dir),
            }
        rows, provider_session_id, metadata = pi_transcript_rows(transcript)
        invocation_rows = [row for row in rows if int(row.get("source_offset") or 0) >= prior_size]
        projection = project_canonical_events_for_harness(
            package=package,
            provider=self.config.provider,
            rows=invocation_rows,
            provider_session_id=provider_session_id,
        )
        assistant_rows = [row for row in invocation_rows if row.get("role") == "assistant" and str(row.get("text") or "").strip()]
        assistant_text = " ".join(_trunc(str(row.get("text") or "")) for row in assistant_rows)
        requested_model = pi_qualification_model()
        evidence_rows = [
            {
                "type": row.get("type"),
                "role": row.get("role"),
                "text": _trunc(str(row.get("text") or "")),
                "timestamp": row.get("timestamp"),
            }
            for row in rows
        ]
        marker_matched = marker in assistant_text
        transcript_bound = bool(provider_session_id) and bool(metadata.get("has_header"))
        evidence = {
            "status": STATUS_PASS,
            "argv": command,
            "returncode": result.returncode,
            "timed_out": timed_out,
            "elapsed_secs": elapsed,
            "transcript_path": transcript_path,
            "transcript_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
            "session_lines": metadata.get("lines"),
            "requested_model": requested_model,
            "observed_model": metadata.get("model") or None,
            "model_honored": bool(metadata.get("model")) and metadata.get("model") == requested_model,
            "provider_session_id": provider_session_id,
            "rows": evidence_rows,
            "invocation_rows": [
                {
                    "type": row.get("type"),
                    "role": row.get("role"),
                    "text": _trunc(str(row.get("text") or "")),
                    "tool_call_id": row.get("tool_call_id"),
                    "tool_name": row.get("tool_name"),
                    "source_offset": row.get("source_offset"),
                }
                for row in invocation_rows
            ],
            "marker": marker,
            "marker_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            "marker_in_prompt": marker in prompt,
            "marker_matched": marker_matched,
            "assistant_row_count": len(assistant_rows),
            "transcript_bound": transcript_bound,
            "canonical_projection": projection,
            "native_taxonomy": metadata.get("taxonomy", {}),
            "native_shadow_taxonomy": pi_native_shadow_taxonomy(invocation_rows, metadata),
            "source_file_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
            "session_file": transcript_path,
            "exact_resume_file": resume_file is not None,
        }
        if result.returncode != 0 and not timed_out:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_run_failed"
            evidence["message"] = f"real pi -p exited {result.returncode} ({_trunc(stderr, 400)})"
        elif timed_out:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_timed_out"
            evidence["message"] = "real pi -p did not finish within the run timeout"
        elif projection.get("status") != STATUS_PASS:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_projection_failed"
            evidence["message"] = str(projection.get("message") or "pi transcript projection did not pass")
        elif not transcript_bound:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_transcript_unbound"
            evidence["message"] = "pi transcript had no session header/id to bind to the Longhouse session"
        elif resume_file is not None and provider_session_id != self._provider_session_id:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_resume_identity_changed"
            evidence["message"] = "Pi exact resume opened a different native session header"
        elif not assistant_rows:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_assistant_row_missing"
            evidence["message"] = "real pi run produced no assistant message row"
        elif not marker_matched:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_marker_missing"
            evidence["message"] = "real pi assistant text did not include the requested marker"
        if evidence.get("status") == STATUS_PASS:
            self._session_file = transcript
            self._provider_session_id = provider_session_id
        return evidence

    def terminate_cleanup(self, package: EvidencePackage) -> dict[str, Any]:
        if not self._use_live():
            payload = self._unsupported_payload(
                "terminate_cleanup",
                "terminate_cleanup_not_safe_no_token",
                "terminate_cleanup requires a live pi turn to prove termination of a real child.",
            )
            package.write_json("assertions/terminate_cleanup.json", payload)
            return payload
        binary, error = self._resolve_untyped_binary(package, "terminate_cleanup")
        if error is not None:
            return error
        session_dir = package.path("pi", "sessions")
        session_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(binary),
            "-p",
            "Think slowly for a long time, then reply with OK.",
            "--provider",
            PI_PROVIDER,
            "--model",
            pi_qualification_model(),
            "--session-dir",
            str(session_dir),
            "--session-id",
            str(uuid.uuid4()),
        ]
        process = subprocess.Popen(
            command,
            cwd=str(package.path("workspace")),
            env=self._pi_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        process_group_id = process.pid
        # Wait for the child to be alive (in flight) so we are terminating a
        # live process, not a corpse.
        in_flight = _wait_alive(process, timeout_secs=PI_INTERRUPT_WAIT_SECS)
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=30)
            reaped = True
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
            reaped = False
        process_group_dead = _wait_process_group_dead(process_group_id)
        if not process_group_dead:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process_group_dead = _wait_process_group_dead(process_group_id)
        payload = {
            "status": STATUS_PASS if (reaped and process.returncode is not None and process_group_dead) else STATUS_FAIL,
            "failure_code": None if (reaped and process.returncode is not None and process_group_dead) else "pi_terminate_not_reaped",
            "message": None
            if (reaped and process.returncode is not None and process_group_dead)
            else "pi child or its process group was not reaped after terminate",
            "terminate_signal": "SIGTERM",
            "provider_pid": process.pid,
            "process_group_id": process_group_id,
            "reaped": reaped,
            "process_group_dead": process_group_dead,
            "returncode": process.returncode,
            "in_flight_process": in_flight,
            "stdout_tail": _trunc(_scrub(stdout or "", os.environ.get("OPENROUTER_API_KEY")), 400),
            "stderr_tail": _trunc(_scrub(stderr or "", os.environ.get("OPENROUTER_API_KEY")), 400),
        }
        package.write_json("assertions/terminate_cleanup.json", payload)
        return payload

    def launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
        if not self._use_live():
            return super().launch_managed_session(package)
        marker = f"LONGHOUSE_PI_LAUNCH_{os.urandom(4).hex()}"
        payload = self._run_pi_turn(package, f"Reply with exactly {marker} and nothing else.", marker)
        package.write_json("assertions/launch_managed_session.json", payload)
        return payload

    def send_receive(self, package: EvidencePackage, prompt: str) -> dict[str, Any]:
        if not self._use_live():
            return super().send_receive(package, prompt)
        marker = f"LONGHOUSE_PI_SEND_{os.urandom(4).hex()}"
        # Keep the prompt single-line: the export_contains_raw assertion matches
        # the raw user text against the exported JSONL, where embedded newlines
        # are JSON-escaped and would break the substring check.
        resolved_prompt = f"{prompt} Include the marker {marker} verbatim in your reply."
        payload = self._run_pi_turn(package, resolved_prompt, marker, resume_file=self._session_file)
        if self._session_file is None:
            payload["status"] = STATUS_FAIL
            payload["failure_code"] = "pi_exact_resume_source_missing"
        package.write_json("assertions/send_receive.json", payload)
        return payload

    def tool_call_result(self, package: EvidencePackage) -> dict[str, Any]:
        if not self._use_live():
            payload = self._unsupported_payload(
                "tool_call_result",
                "tool_call_result_not_safe_no_token",
                "Pi tool-call proof requires an explicit live model turn.",
            )
            package.write_json("assertions/tool_call_result.json", payload)
            return payload
        marker = f"LONGHOUSE_PI_TOOL_{os.urandom(4).hex()}"
        prompt = f"Use the bash tool to run printf PI_TOOL_PROOF, then reply with exactly {marker} and nothing else."
        payload = self._run_pi_turn(package, prompt, marker, resume_file=self._session_file)
        rows = payload.get("invocation_rows") if isinstance(payload.get("invocation_rows"), list) else []
        payload["tool_call_count"] = sum(1 for row in rows if row.get("tool_call_id") and row.get("role") == "assistant")
        payload["tool_result_count"] = sum(1 for row in rows if row.get("type") == "tool_result")
        payload["tool_output_marker_observed"] = any(
            row.get("type") == "tool_result" and "PI_TOOL_PROOF" in str(row.get("text") or "") for row in rows
        )
        taxonomy = payload.get("native_shadow_taxonomy") if isinstance(payload.get("native_shadow_taxonomy"), dict) else {}
        payload["tool_pair_count"] = len(taxonomy.get("tool_pairs") or [])
        if payload.get("status") == STATUS_PASS and (
            not payload["tool_call_count"]
            or not payload["tool_result_count"]
            or not payload["tool_pair_count"]
            or not payload["tool_output_marker_observed"]
        ):
            payload["status"] = STATUS_FAIL
            payload["failure_code"] = "pi_tool_pair_missing"
            payload["message"] = "Pi native JSONL did not contain both the tool call and tool result."
        package.write_json("assertions/tool_call_result.json", payload)
        return payload

    def cold_resume(self, package: EvidencePackage) -> dict[str, Any]:
        if self._session_file is None:
            payload = self._unsupported_payload(
                "helm_cold_resume_native",
                "pi_exact_resume_source_missing",
                "Pi exact resume requires a completed native session from this qualification run.",
            )
        else:
            marker = f"LONGHOUSE_PI_RESUME_{os.urandom(4).hex()}"
            payload = self._run_pi_turn(
                package,
                f"Reply with exactly {marker} and nothing else.",
                marker,
                resume_file=self._session_file,
            )
            payload["proof_scope"] = "console_exact_native_resume"
        package.write_json("assertions/helm_cold_resume_native.json", payload)
        return payload

    def interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        if not self._use_live():
            payload = self._unsupported_payload(
                "interrupt_cancel",
                "interrupt_cancel_not_safe_no_token",
                "interrupt_cancel requires a live pi turn to prove mid-run interruption.",
            )
            package.write_json("assertions/interrupt_cancel.json", payload)
            return payload
        binary, error = self._resolve_untyped_binary(package, "interrupt_cancel")
        if error is not None:
            return error
        session_dir = package.path("pi", "sessions")
        session_dir.mkdir(parents=True, exist_ok=True)
        interrupt_session_id = str(uuid.uuid4())
        command = [
            str(binary),
            "-p",
            # A long-generation prompt keeps pi in flight long enough (flash can
            # finish a short reply before an interrupt window opens); the essay
            # request forces a multi-second generation we can interrupt mid-way.
            "Write a detailed 500-word essay about the history of computing.",
            "--provider",
            PI_PROVIDER,
            "--model",
            pi_qualification_model(),
            "--session-dir",
            str(session_dir),
            "--session-id",
            interrupt_session_id,
        ]
        process = subprocess.Popen(
            command,
            cwd=str(package.path("workspace")),
            env=self._pi_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # Prove the turn is in flight (process alive) before signaling; if pi
        # exited first, the interrupt was not exercised.
        in_flight = _wait_alive(process, timeout_secs=PI_INTERRUPT_WAIT_SECS)
        if in_flight is None:
            process.kill()
            process.communicate(timeout=10)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process_group_dead = _wait_process_group_dead(process.pid)
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "pi_interrupt_not_in_flight",
                "message": "pi exited before an interrupt could be delivered; cannot prove mid-run interruption",
                "provider_pid": process.pid,
                "process_group_id": process.pid,
                "process_group_dead": process_group_dead,
            }
            package.write_json("assertions/interrupt_cancel.json", payload)
            return payload
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=30)
            terminated = True
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
            terminated = False
        process_group_dead = _wait_process_group_dead(process.pid)
        if not process_group_dead:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process_group_dead = _wait_process_group_dead(process.pid)
        transcript = session_file_for_id(session_dir, interrupt_session_id)
        killed_by_signal = process.returncode is not None and process.returncode < 0
        in_flight_transcript = transcript is not None
        passed = bool(in_flight and terminated and in_flight_transcript and process.returncode is not None and process_group_dead)
        payload = {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "failure_code": None if passed else "pi_interrupt_evidence_missing",
            "message": None if passed else "pi did not die from SIGINT while in flight",
            "interrupt_signal": "SIGINT",
            "terminated": terminated,
            "returncode": process.returncode,
            "killed_by_signal": killed_by_signal,
            "in_flight_transcript": in_flight_transcript,
            "transcript_present": transcript is not None,
            "provider_pid": process.pid,
            "process_group_id": process.pid,
            "process_group_dead": process_group_dead,
            "stdout_tail": _trunc(_scrub(stdout or "", os.environ.get("OPENROUTER_API_KEY")), 400),
            "stderr_tail": _trunc(_scrub(stderr or "", os.environ.get("OPENROUTER_API_KEY")), 400),
        }
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload


def _process_group_dead(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_process_group_dead(pgid: int, timeout_secs: float = 10) -> bool:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if _process_group_dead(pgid):
            return True
        time.sleep(0.1)
    return _process_group_dead(pgid)


def _wait_alive(process: subprocess.Popen[str], timeout_secs: float) -> bool:
    """Poll until the process is (or was) observed alive; returns True if it
    was running at any point before the timeout, False if it exited immediately.
    Used to prove the child was in flight before an interrupt/terminate."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if process.poll() is None:
            return True
        # process exited; give it a final settle then report
        time.sleep(0.2)
    return process.poll() is None
