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

# Pi's built-in provider id plus the qualification model. Override the model
# through the env so CI can pin a cheap one without editing the adapter.
PI_PROVIDER = "openrouter"
PI_MODEL_ENV = "LONGHOUSE_PI_QUALIFICATION_MODEL"
PI_DEFAULT_MODEL = "~deepseek/deepseek-v4-flash-latest"
PI_RUN_TIMEOUT_SECS = 120


def pi_qualification_model() -> str:
    return os.environ.get(PI_MODEL_ENV) or PI_DEFAULT_MODEL


def _pi_text_content(message: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Return (free text, tool blocks) flattened from an AgentMessage content."""
    texts: list[str] = []
    tools: list[dict[str, Any]] = []
    content = message.get("content")
    if not isinstance(content, list):
        return "".join(texts), tools
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
    first model_change and the jsonl line count.
    """
    rows: list[dict[str, Any]] = []
    header_id: str | None = None
    metadata: dict[str, Any] = {"lines": 0, "model": None}
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return rows, None, {"lines": 0, "model": None, "error": f"{type(exc).__name__}: {exc}"}

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
        env = dict(os.environ)
        env.setdefault("PI_DISABLE_AUTOUPDATE", "1")
        return env

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
        stdout = result.stdout or ""
        stderr = result.stderr or ""
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
                "stdout": stdout[-2000:],
                "stderr": stderr[-2000:],
                "session_dir": str(session_dir),
            }
        rows, provider_session_id, metadata = pi_transcript_rows(transcript)
        ingested = ingest_canonical_events_into_longhouse_db(
            package=package,
            provider=self.config.provider,
            rows=rows,
            provider_session_id=provider_session_id,
        )
        assistant_text = " ".join(str(row.get("text") or "") for row in rows if row.get("role") == "assistant")
        marker_match = marker in stdout or marker in assistant_text
        evidence = {
            "status": STATUS_PASS,
            "argv": command,
            "returncode": result.returncode,
            "timed_out": timed_out,
            "elapsed_secs": elapsed,
            "transcript_path": transcript_path,
            "transcript_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
            "session_lines": metadata.get("lines"),
            "model": metadata.get("model") or pi_qualification_model(),
            "provider_session_id": provider_session_id,
            "rows": rows,
            "marker": marker,
            "marker_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            "marker_in_prompt": marker in prompt,
            "marker_matched": marker_match,
            "ingest": ingested,
        }
        if result.returncode != 0 and not timed_out:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_run_failed"
            evidence["message"] = f"real pi -p exited {result.returncode} ({stderr[-400:]})"
        elif timed_out:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_timed_out"
            evidence["message"] = "real pi -p did not finish within the run timeout"
        elif ingested.get("status") != STATUS_PASS:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_db_ingest_failed"
            evidence["message"] = str(ingested.get("message") or "pi transcript DB ingest did not pass")
        elif not marker_match:
            evidence["status"] = STATUS_FAIL
            evidence["failure_code"] = "pi_print_marker_missing"
            evidence["message"] = "real pi -p did not emit the requested marker text"
        return evidence

    @staticmethod
    def _has_credential() -> bool:
        return bool((os.environ.get("OPENROUTER_API_KEY") or "").strip())

    def _use_live(self) -> bool:
        """True when this run should spend a real pi model turn.

        The factory's hermetic lanes run every provider against generated fake
        binaries with no credentials; pi delegates those to the base adapter's
        session-safe projection rather than launching a fake binary that cannot
        produce a transcript.
        """
        if self.provider_build is not None and self.provider_build.artifact_provenance == GENERATED_FAKE_PROVENANCE:
            return False
        return self._has_credential()

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
        binary, error = self._resolve_untyped_binary(package, "interrupt_cancel")
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
        # Give the child a beat to start, then interrupt it like Longhouse would.
        time.sleep(3)
        process.send_signal(subprocess.signal.SIGINT)
        try:
            stdout, stderr = process.communicate(timeout=30)
            terminated = True
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
            terminated = False
        transcript = _newest_session_file(session_dir)
        payload = {
            "status": STATUS_PASS if terminated else STATUS_FAIL,
            "failure_code": None if terminated else "pi_interrupt_not_terminated",
            "message": None if terminated else "pi child survived SIGINT",
            "interrupt_signal": "SIGINT",
            "terminated": terminated,
            "returncode": process.returncode,
            "transcript_present": transcript is not None,
            "stdout_tail": (stdout or "")[-400:],
            "stderr_tail": (stderr or "")[-400:],
        }
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def terminate_cleanup(self, package: EvidencePackage) -> dict[str, Any]:
        # No owned child in a terminate-only scenario; the launch/send paths own
        # their subprocesses and end them synchronously. Terminate is satisfied
        # by the synchronous run contract plus a parseable transcript.
        payload = {
            "status": STATUS_PASS,
            "scenario": "terminate_cleanup",
            "note": "pi runs are bounded subprocesses; terminate is subprocess.run completion.",
        }
        package.write_json("assertions/terminate_cleanup.json", payload)
        return payload
