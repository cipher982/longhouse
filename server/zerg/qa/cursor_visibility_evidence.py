"""Correlate Cursor transcript, hook, and rendered-terminal evidence.

This module is intentionally an observation tool.  It does not decide which
provider artifacts belong in the Longhouse transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pyte


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _message_text(row: dict[str, Any]) -> str:
    message = row.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text"))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


def transcript_turns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project JSONL rows into turns without collapsing assistant artifacts."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index, row in enumerate(rows):
        role = row.get("role")
        if role == "user":
            current = {
                "user_jsonl_index": index,
                "user_text_sha256": _sha256(_message_text(row)),
                "assistant_artifacts": [],
                "terminal_status": None,
                "terminal_error": None,
            }
            turns.append(current)
        elif role == "assistant" and current is not None:
            text = _message_text(row)
            if text:
                artifact = {"jsonl_index": index, "text": text, "text_sha256": _sha256(text)}
                current["assistant_artifacts"].append(artifact)
        elif row.get("type") == "turn_ended" and current is not None:
            current["terminal_status"] = row.get("status")
            current["terminal_error"] = row.get("error")
    return turns


def render_terminal(raw: bytes, *, columns: int, lines: int) -> list[str]:
    """Replay PTY bytes into a VT-compatible final screen."""
    screen = pyte.Screen(columns, lines)
    stream = pyte.ByteStream(screen)
    stream.feed(raw)
    return [line.rstrip() for line in screen.display]


def _frame_occurrences(text: str, display: list[str]) -> int:
    wanted = [line.strip() for line in text.splitlines()]
    if not wanted or any(not line for line in wanted):
        return 0
    actual = [line.strip() for line in display]
    width = len(wanted)
    return sum(actual[index : index + width] == wanted for index in range(len(actual) - width + 1))


def _indices_for_digest(artifacts: list[dict[str, Any]], digest: str) -> list[int]:
    indices: list[int] = []
    for artifact in artifacts:
        if artifact["text_sha256"] == digest:
            indices.append(int(artifact["jsonl_index"]))
    return indices


def build_visibility_report(
    *,
    transcript_rows: list[dict[str, Any]],
    hook_rows: list[dict[str, Any]],
    terminal_display: list[str],
    provider_conversation_id: str | None = None,
) -> dict[str, Any]:
    """Return mechanical correlations while preserving ambiguous bindings."""
    scoped_hooks = []
    for row in hook_rows:
        if provider_conversation_id is None or row.get("conversation_id") == provider_conversation_id:
            scoped_hooks.append(row)
    receipt_counts: Counter[str] = Counter()
    for row in scoped_hooks:
        if row.get("event") == "afterAgentResponse" and row.get("text_sha256"):
            receipt_counts[str(row["text_sha256"])] += 1
    turns = transcript_turns(transcript_rows)
    all_artifact_counts: Counter[str] = Counter()
    for turn in turns:
        for artifact in turn["assistant_artifacts"]:
            all_artifact_counts[str(artifact["text_sha256"])] += 1
    for turn in turns:
        artifacts = turn.pop("assistant_artifacts")
        counts = Counter(str(artifact["text_sha256"]) for artifact in artifacts)
        first_by_hash = {str(artifact["text_sha256"]): artifact for artifact in artifacts}
        groups: list[dict[str, Any]] = []
        for digest, count in counts.items():
            artifact = first_by_hash[digest]
            occurrences = _frame_occurrences(str(artifact["text"]), terminal_display)
            receipt_count = receipt_counts[digest]
            if receipt_count and all_artifact_counts[digest] == 1:
                correlation = "provider_commit_receipt_unique_artifact"
            elif receipt_count:
                correlation = "provider_commit_receipt_ambiguous_artifacts"
            elif occurrences and count == 1:
                correlation = "terminal_presented_unique_artifact"
            elif occurrences:
                correlation = "terminal_presented_ambiguous_artifacts"
            else:
                correlation = "provider_artifact_only"
            groups.append(
                {
                    "text_sha256": digest,
                    "artifact_count": count,
                    "jsonl_indices": _indices_for_digest(artifacts, digest),
                    "after_agent_response_receipt_count": receipt_count,
                    "final_terminal_frame_occurrences": occurrences,
                    "correlation": correlation,
                }
            )
        turn["assistant_artifact_count"] = len(artifacts)
        turn["assistant_content_groups"] = groups
    return {
        "schema_version": 1,
        "contract": "cursor_visibility_observations",
        "provider_conversation_id": provider_conversation_id,
        "terminal_final_frame": terminal_display,
        "turns": turns,
        "notes": [
            "Correlations are evidence labels, not transcript inclusion decisions.",
            "A final frame cannot prove content was never visible in an earlier frame.",
            "Repeated artifacts with identical text cannot be ordinally bound to one rendered line.",
            "A content digest match does not bind a receipt to a turn when multiple artifacts share that digest.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--hooks", type=Path, required=True)
    parser.add_argument("--terminal", type=Path, required=True)
    parser.add_argument("--conversation-id", help="Defaults to the transcript filename stem")
    parser.add_argument("--columns", type=int, default=132)
    parser.add_argument("--lines", type=int, default=40)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_visibility_report(
        transcript_rows=_read_jsonl(args.transcript),
        hook_rows=_read_jsonl(args.hooks),
        terminal_display=render_terminal(args.terminal.read_bytes(), columns=args.columns, lines=args.lines),
        provider_conversation_id=args.conversation_id or args.transcript.stem,
    )
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
