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

Only the text path is exercised today (``--no-tools``); tool blocks are mapped
forwards when present but the first slice does not prove a live tool call.

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
import subprocess
import time
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.qa.provider_build_store import GENERATED_FAKE_PROVENANCE
from zerg.qa.universal_agent_harness import STATUS_FAIL
from zerg.qa.universal_agent_harness import STATUS_PASS
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import ingest_canonical_events_into_longhouse_db
from zerg.qa.universal_agent_harness import register_adapter

# Pi's built-in provider id plus the qualification model. The model is
# overridable through the env so CI can pin a concrete one without editing the
# adapter; the default is a floating -latest alias, so it is cd-only.
PI_PROVIDER = "openrouter"
PI_MODEL_ENV = "LONGHOUSE_PI_QUALIFICATION_MODEL"
PI_DEFAULT_MODEL = "~deepseek/deepseek-v4-flash-latest"
PI_LIVE_ENV = "LONGHOUSE_PI_LIVE"
PI_RUN_TIMEOUT_SECS = 120
PI_INTERRUPT_WAIT_SECS = 20
PI_EVIDENCE_TEXT_LIMIT = 2000


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


def _pi_text_content(message: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Return (free text, tool blocks) flattened from an AgentMessage content."""
    texts: list[str] = []
    tools: list[dict[str, Any]] = []
    content = message.get("content")
    if not isinstance(content, list):
        return "\n".join(texts), tools
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind in {"tool_call", "tool_result", "tool"}:
            tools.append(dict(block))
    return "\n".join(texts), tools


def pi_transcript_rows(transcript: Path) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Parse a Pi session JSONL into Longhouse raw-event rows.

    Returns ``(rows, provider_session_id, metadata)`` where metadata carries the
    first model_change, the jsonl line count, and whether a session header was
    seen (required for a valid transcript binding).
    """
    rows: list[dict[str, Any]] = []
    header_id: str | None = None
    metadata: dict[str, Any] = {"lines": 0, "model": None, "has_header": False}
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return rows, None, {"lines": 0, "model": None, "has_header": False, "error": f"{type(exc).__name__}: {exc}"}

    for line in lines:
        if not line.strip():
            continue
        metadata["lines"] += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "")
        if kind == "session":
            header_id = str(entry.get("id") or "") or header_id
            if header_id:
                metadata["has_header"] = True
        elif kind == "message":
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text, tools = _pi_text_content(message)
            timestamp = entry.get("timestamp")
            if role == "user":
                row: dict[str, Any] = {"type": "user", "role": "user", "text": text, "timestamp": timestamp}
            else:
                row = {"type": "assistant", "role": "assistant", "text": text, "timestamp": timestamp}
                if tools:
                    # First slice: record tool intent on the assistant row.
                    row["tool_name"] = "pi_tool"
                    row["tool_input_json"] = {"blocks": tools}
            if header_id:
                row["provider_session_id"] = header_id
            rows.append(row)
        elif kind == "model_change" and metadata.get("model") is None:
            metadata["model"] = entry.get("modelId")
    return rows, header_id, metadata


def _newest_session_file(session_dir: Path) -> Path | None:
    if not session_dir.is_dir():
        return None
    candidates = [path for path in session_dir.iterdir() if path.is_file() and path.name.endswith(".jsonl")]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


@register_adapter("pi")
class PiHarnessAdapter(UniversalProviderAdapter):
    """Pi concrete adapter for the universal Longhouse action contract."""

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

    def _run_pi_turn(self, package: EvidencePackage, prompt: str, marker: str) -> dict[str, Any]:
        binary, error = self._resolve_untyped_binary(package, "pi_turn")
        if error is not None:
            return error
        session_dir = package.path("pi", "sessions")
        session_dir.mkdir(parents=True, exist_ok=True)
        workdir = package.path("workspace")
        workdir.mkdir(parents=True, exist_ok=True)
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
            "--no-context-files",
            "--no-tools",
        ]
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
        transcript = _newest_session_file(session_dir)
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
        ingested = ingest_canonical_events_into_longhouse_db(
            package=package,
            provider=self.config.provider,
            rows=rows,
            provider_session_id=provider_session_id,
        )
        assistant_rows = [row for row in rows if row.get("role") == "assistant" and str(row.get("text") or "").strip()]
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
            "marker": marker,
            "marker_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            "marker_in_prompt": marker in prompt,
            "marker_matched": marker_matched,
            "assistant_row_count": len(assistant_rows),
            "transcript_bound": transcript_bound,
            "ingest": ingested,
        }
        if result.returncode != 0 and not timed_out:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_run_failed"
            evidence["message"] = f"real pi -p exited {result.returncode} ({_trunc(stderr, 400)})"
        elif timed_out:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_timed_out"
            evidence["message"] = "real pi -p did not finish within the run timeout"
        elif ingested.get("status") != STATUS_PASS:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_db_ingest_failed"
            evidence["message"] = str(ingested.get("message") or "pi transcript DB ingest did not pass")
        elif not transcript_bound:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_transcript_unbound"
            evidence["message"] = "pi transcript had no session header/id to bind to the Longhouse session"
        elif not assistant_rows:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_assistant_row_missing"
            evidence["message"] = "real pi run produced no assistant message row"
        elif not marker_matched:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_marker_missing"
            evidence["message"] = "real pi assistant text did not include the requested marker"
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
            "--no-context-files",
            "--no-tools",
        ]
        process = subprocess.Popen(
            command,
            cwd=str(package.path("workspace")),
            env=self._pi_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
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
        payload = {
            "status": STATUS_PASS if (reaped and process.returncode is not None) else STATUS_FAIL,
            "failure_code": None if (reaped and process.returncode is not None) else "pi_terminate_not_reaped",
            "message": None if (reaped and process.returncode is not None) else "pi child was not reaped after terminate",
            "terminate_signal": "SIGTERM",
            "reaped": reaped,
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
        payload = self._run_pi_turn(package, resolved_prompt, marker)
        package.write_json("assertions/send_receive.json", payload)
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
        command = [
            str(binary),
            "-p",
            # A long-generation prompt keeps pi in flight long enough (flash can
            # finish a short reply before an interrupt window opens); the essay
            # request forces a multi-second generation we can interrupt mid-way.
            "Write a detailed 500-word essay about the history of computing. Do not use tools.",
            "--provider",
            PI_PROVIDER,
            "--model",
            pi_qualification_model(),
            "--session-dir",
            str(session_dir),
            "--no-context-files",
            "--no-tools",
        ]
        process = subprocess.Popen(
            command,
            cwd=str(package.path("workspace")),
            env=self._pi_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Prove the turn is in flight (process alive) before signaling; if pi
        # exited first, the interrupt was not exercised.
        in_flight = _wait_alive(process, timeout_secs=PI_INTERRUPT_WAIT_SECS)
        if in_flight is None:
            process.kill()
            process.communicate(timeout=10)
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "pi_interrupt_not_in_flight",
                "message": "pi exited before an interrupt could be delivered; cannot prove mid-run interruption",
            }
            package.write_json("assertions/interrupt_cancel.json", payload)
            return payload
        process.send_signal(subprocess.signal.SIGINT)
        try:
            stdout, stderr = process.communicate(timeout=30)
            terminated = True
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
            terminated = False
        transcript = _newest_session_file(session_dir)
        killed_by_signal = process.returncode is not None and process.returncode < 0
        in_flight_transcript = transcript is not None
        passed = killed_by_signal and in_flight
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
            "stdout_tail": _trunc(_scrub(stdout or "", os.environ.get("OPENROUTER_API_KEY")), 400),
            "stderr_tail": _trunc(_scrub(stderr or "", os.environ.get("OPENROUTER_API_KEY")), 400),
        }
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload


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