#!/usr/bin/env python3
"""Export and LLM-classify historical Hatch automation candidates.

This is intentionally a review artifact generator, not a mutator. It writes
candidate rows and model decisions to disk; the real DB repair still goes
through `longhouse db classify-automation --session-id ... --apply`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MODEL = "deepseek/deepseek-v4-flash"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

EXPORT_FIELDS = [
    "session_id",
    "started_at",
    "provider",
    "project",
    "device_id",
    "environment",
    "cwd",
    "git_branch",
    "execution_home",
    "managed_transport",
    "is_sidechain",
    "user_messages",
    "assistant_messages",
    "tool_calls",
    "title",
    "first_user_message",
]

DECISION_FIELDS = [
    "session_id",
    "classification",
    "confidence",
    "apply",
    "reason",
    "signals",
    "provider",
    "started_at",
    "title",
]

CLASSIFIER_SYSTEM_PROMPT = """\
You classify historical Longhouse agent sessions for a one-time cleanup.

Longhouse is a timeline of real CLI agent sessions. Hatch is David's helper
launcher for delegated one-off agent work. Hatch child runs are valuable archive
artifacts, but they should be hidden from the default human timeline as
`hatch_automation`.

You get one or more candidate rows. Each row is already shaped like a possible
one-shot session: root thread, provider in Claude/Codex/OpenCode/Cursor, and one
user message. Decide whether each row should be marked as Hatch automation.

Classify as `hatch_automation` when the first user message clearly looks like a
delegated subtask given to another agent, especially:
- independent code review, final review, quick review, phase review, audit
- drill down, inspect, verify, critique, sanity-check, summarize findings
- "review this branch/diff/implementation/spec/plan", "look for blockers"
- an instruction that references another agent's work, current branch, PR,
  implementation, pasted plan, or asks for a second opinion
- agent-to-agent phrasing like "You are reviewing...", "do not modify files",
  "report findings", "return JSON", "compile/sort these", "from first principles"

Classify as `normal_user_session` when it reads like David directly starting or
continuing a real task, even if it was short:
- build, fix, implement, debug, design, explain, ship, deploy, run tests
- conversational follow-up, product decision, or question to the main agent
- direct live-session steering such as "continue", "stop", "what happened"

Classify as `test_or_canary` for explicit smoke tests, E2E checks, provider
proofs, launch probes, synthetic fixtures, or benchmark prompts.

Classify as `provider_subagent` only when the text itself clearly says it is a
provider-native subagent/sidechain transcript rather than Hatch automation.

When unsure, choose `uncertain`. This cleanup is fail-visible: false positives
are worse than leaving a session visible.

For a single row, return strict JSON only:
{
  "classification": "hatch_automation" | "normal_user_session" | "test_or_canary" | "provider_subagent" | "uncertain",
  "confidence": "high" | "medium" | "low",
  "apply": true | false,
  "reason": "short reason, no more than 18 words",
  "signals": ["short", "evidence", "terms"]
}

For multiple rows, return strict JSON only:
{
  "decisions": [
    {
      "session_id": "...",
      "classification": "hatch_automation" | "normal_user_session" | "test_or_canary" | "provider_subagent" | "uncertain",
      "confidence": "high" | "medium" | "low",
      "apply": true | false,
      "reason": "short reason, no more than 18 words",
      "signals": ["short", "evidence", "terms"]
    }
  ]
}

Set `apply=true` only for `hatch_automation` or `test_or_canary` with high or
medium confidence. Otherwise set `apply=false`.
"""

APPLY_CLASSIFICATIONS = {"hatch_automation", "test_or_canary"}


REMOTE_EXPORT_SCRIPT = r"""
import csv
import sqlite3
import sys

db_uri = "file:/data/longhouse.db?mode=ro"
con = sqlite3.connect(db_uri, uri=True, timeout=15)
con.row_factory = sqlite3.Row

fields = __FIELDS__
writer = csv.DictWriter(sys.stdout, fieldnames=fields)
writer.writeheader()

query = '''
select
  s.id as session_id,
  s.started_at,
  s.provider,
  s.project,
  s.device_id,
  s.environment,
  s.cwd,
  s.git_branch,
  s.execution_home,
  s.managed_transport,
  s.is_sidechain,
  coalesce(s.user_messages, 0) as user_messages,
  coalesce(s.assistant_messages, 0) as assistant_messages,
  coalesce(s.tool_calls, 0) as tool_calls,
  coalesce(s.anchor_title, s.summary_title, '') as title,
  coalesce(
    nullif(trim(s.first_user_message), ''),
    nullif(trim(s.first_user_message_preview), ''),
    (
      select e.content_text
      from events e
      where e.session_id = s.id
        and e.role = 'user'
        and length(trim(coalesce(e.content_text, ''))) > 0
      order by e.timestamp asc, e.id asc
      limit 1
    ),
    ''
  ) as first_user_message
from sessions s
join session_threads t on t.session_id = s.id and t.is_primary = 1
where t.branch_kind = 'root'
  and s.provider in ('opencode', 'cursor', 'claude', 'codex')
  and __USER_MESSAGE_PREDICATE__
  and coalesce(s.environment, '') not in ('test', 'e2e')
order by s.started_at desc, s.id
'''

for row in con.execute(query):
    writer.writerow({field: row[field] for field in fields})
"""


def _die(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _write_decision_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in DECISION_FIELDS})


def _load_existing_decisions(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    decisions: dict[str, dict[str, Any]] = {}
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(row.get("session_id") or "")
            if session_id:
                decisions[session_id] = row
    return decisions


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _candidate_payload(row: dict[str, str]) -> dict[str, Any]:
    return {
        "session_id": row.get("session_id", ""),
        "provider": row.get("provider", ""),
        "project": row.get("project", ""),
        "started_at": row.get("started_at", ""),
        "environment": row.get("environment", ""),
        "execution_home": row.get("execution_home", ""),
        "managed_transport": row.get("managed_transport", ""),
        "is_sidechain": row.get("is_sidechain", ""),
        "counts": {
            "user_messages": row.get("user_messages", ""),
            "assistant_messages": row.get("assistant_messages", ""),
            "tool_calls": row.get("tool_calls", ""),
        },
        "title": row.get("title", ""),
        "cwd": row.get("cwd", ""),
        "git_branch": row.get("git_branch", ""),
        "first_user_message": row.get("first_user_message", ""),
    }


def _candidate_user_message(row: dict[str, str]) -> str:
    return "Classify this candidate row:\n" + json.dumps(_candidate_payload(row), ensure_ascii=False)


def _batch_user_message(rows: list[dict[str, str]]) -> str:
    payload = [_candidate_payload(row) for row in rows]
    return "Classify these candidate rows independently:\n" + json.dumps(payload, ensure_ascii=False)


def _normalize_decision(data: dict[str, Any]) -> dict[str, Any]:
    classification = str(data.get("classification") or "uncertain")
    confidence = str(data.get("confidence") or "low")
    apply = (
        bool(data.get("apply")) or classification in APPLY_CLASSIFICATIONS
    ) and classification in APPLY_CLASSIFICATIONS and confidence in {"high", "medium"}
    return {
        "classification": classification,
        "confidence": confidence,
        "apply": apply,
        "reason": str(data.get("reason") or "")[:180],
        "signals": data.get("signals") if isinstance(data.get("signals"), list) else [],
    }


def _error_decision(message: str) -> dict[str, Any]:
    payload = {
        "classification": "uncertain",
        "confidence": "low",
        "apply": False,
        "reason": "classifier_error",
        "signals": [message[:120]],
    }
    return payload


def _parse_model_json(content: str | None) -> dict[str, Any]:
    if not content:
        raise ValueError("model returned empty content")
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("model returned non-object JSON")
    return _normalize_decision(data)


def _parse_batch_json(content: str | None, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not content:
        raise ValueError("model returned empty content")
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise ValueError("model returned batch JSON without decisions[]")

    decisions_by_id: dict[str, dict[str, Any]] = {}
    for item in data["decisions"]:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "")
        if session_id:
            decisions_by_id[session_id] = _normalize_decision(item)

    results: list[dict[str, Any]] = []
    for row in rows:
        session_id = row.get("session_id", "")
        results.append(decisions_by_id.get(session_id) or _error_decision("missing batch decision"))
    return results


def _call_openrouter(
    *,
    api_key: str,
    row: dict[str, str],
    model: str,
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": _candidate_user_message(row)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request_body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cipher982/longhouse",
        "X-Title": "Longhouse Hatch Backfill Classifier",
    }

    last_error = ""
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(OPENROUTER_URL, data=request_body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = json.loads(response.read())
            content = body["choices"][0]["message"]["content"]
            decision = _parse_model_json(content)
            usage = body.get("usage") or {}
            return {
                **decision,
                "model": model,
                "routed_model": body.get("model"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            }
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            KeyError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                detail = exc.read().decode(errors="replace")[:500]
                last_error = f"HTTP {exc.code}: {detail}"
                retryable = exc.code in {408, 409, 425, 429, 500, 502, 503, 504}
            else:
                last_error = f"{type(exc).__name__}: {exc}"
                retryable = True
            if attempt >= max_retries or not retryable:
                break
            time.sleep(min(8, 0.75 * (2**attempt)))

    return {
        **_error_decision(last_error),
        "model": model,
        "error": last_error,
    }


def _call_openrouter_batch(
    *,
    api_key: str,
    rows: list[dict[str, str]],
    model: str,
    timeout: float,
    max_retries: int,
) -> list[dict[str, Any]]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": _batch_user_message(rows)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request_body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cipher982/longhouse",
        "X-Title": "Longhouse Hatch Backfill Classifier",
    }

    last_error = ""
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(OPENROUTER_URL, data=request_body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = json.loads(response.read())
            content = body["choices"][0]["message"]["content"]
            decisions = _parse_batch_json(content, rows)
            usage = body.get("usage") or {}
            return [
                {
                    **decision,
                    "model": model,
                    "routed_model": body.get("model"),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                }
                for decision in decisions
            ]
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            KeyError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                detail = exc.read().decode(errors="replace")[:500]
                last_error = f"HTTP {exc.code}: {detail}"
                retryable = exc.code in {408, 409, 425, 429, 500, 502, 503, 504}
            else:
                last_error = f"{type(exc).__name__}: {exc}"
                retryable = True
            if attempt >= max_retries or not retryable:
                break
            time.sleep(min(8, 0.75 * (2**attempt)))

    return [{**_error_decision(last_error), "model": model, "error": last_error} for _ in rows]


def export_hosted(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    user_message_predicate = (
        "coalesce(s.user_messages, 0) <= 1"
        if args.include_zero_user
        else "coalesce(s.user_messages, 0) = 1"
    )
    remote_script = (
        REMOTE_EXPORT_SCRIPT.replace("__FIELDS__", repr(EXPORT_FIELDS)).replace(
            "__USER_MESSAGE_PREDICATE__", user_message_predicate
        )
    )
    command = [
        "ssh",
        args.ssh_host,
        "docker",
        "exec",
        "-i",
        f"longhouse-{args.subdomain}",
        "python",
        "-",
    ]
    with output.open("w") as out:
        result = subprocess.run(command, input=remote_script, text=True, stdout=out, stderr=subprocess.PIPE)
    if result.returncode != 0:
        _die(f"export failed: {result.stderr.strip()}", result.returncode)
    rows = _read_csv(output)
    print(json.dumps({"status": "ok", "output": str(output), "rows": len(rows)}, sort_keys=True), flush=True)
    return 0


def classify(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    output_jsonl = Path(args.output_jsonl).expanduser()
    output_csv = Path(args.output_csv).expanduser() if args.output_csv else None
    rows = _read_csv(input_path)
    if args.limit:
        rows = rows[: args.limit]

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        _die("OPENROUTER_API_KEY is required for classify")

    existing = _load_existing_decisions(output_jsonl if args.resume else None)
    pending = [row for row in rows if row.get("session_id") not in existing]
    batch_size = max(1, args.batch_size)
    batches = [pending[index : index + batch_size] for index in range(0, len(pending), batch_size)]
    print(
        json.dumps(
            {
                "status": "starting",
                "input": str(input_path),
                "rows": len(rows),
                "already_done": len(existing),
                "pending": len(pending),
                "batches": len(batches),
                "batch_size": batch_size,
                "model": args.model,
                "workers": args.workers,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    def classify_batch(batch: list[dict[str, str]]) -> list[dict[str, Any]]:
        if len(batch) == 1:
            decisions = [
                _call_openrouter(
                    api_key=api_key,
                    row=batch[0],
                    model=args.model,
                    timeout=args.timeout,
                    max_retries=args.retries,
                )
            ]
        else:
            decisions = _call_openrouter_batch(
                api_key=api_key,
                rows=batch,
                model=args.model,
                timeout=args.timeout,
                max_retries=args.retries,
            )
        results = []
        for row, decision in zip(batch, decisions, strict=False):
            results.append(
                {
                    "session_id": row.get("session_id", ""),
                    "provider": row.get("provider", ""),
                    "started_at": row.get("started_at", ""),
                    "title": row.get("title", ""),
                    **decision,
                }
            )
        return results

    completed = 0
    next_progress = args.progress_every
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(classify_batch, batch): batch for batch in batches}
        for future in concurrent.futures.as_completed(futures):
            results = future.result()
            for result in results:
                _append_jsonl(output_jsonl, result)
            completed += len(results)
            if completed >= next_progress or completed == len(pending):
                print(
                    json.dumps({"status": "progress", "completed": completed, "pending": len(pending)}, sort_keys=True),
                    flush=True,
                )
                while next_progress <= completed:
                    next_progress += args.progress_every

    decisions = _load_existing_decisions(output_jsonl)
    ordered = [decisions[row["session_id"]] for row in rows if row.get("session_id") in decisions]
    if output_csv:
        _write_decision_csv(output_csv, ordered)

    counts: dict[str, int] = {}
    apply_count = 0
    for row in ordered:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
        apply_count += 1 if row.get("apply") else 0
    print(
        json.dumps(
            {
                "status": "ok",
                "rows": len(ordered),
                "apply_count": apply_count,
                "counts": counts,
                "output_jsonl": str(output_jsonl),
                "output_csv": str(output_csv) if output_csv else None,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def write_apply_ids(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    output = Path(args.output).expanduser()
    rows = _load_existing_decisions(input_path)
    classification = str(args.classification or "").strip()
    if classification:
        session_ids = sorted(
            session_id
            for session_id, row in rows.items()
            if row.get("classification") == classification and row.get("confidence") in {"high", "medium"}
        )
        selection = "classification_high_medium"
    else:
        session_ids = sorted(session_id for session_id, row in rows.items() if row.get("apply") is True)
        selection = "apply_true"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(session_ids) + ("\n" if session_ids else ""))
    print(
        json.dumps(
            {
                "status": "ok",
                "classification": classification or None,
                "selection": selection,
                "output": str(output),
                "session_ids": len(session_ids),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Example:
              python scripts/ops/hatch_backfill_llm.py export-hosted --output /tmp/hatch-candidates.csv
              OPENROUTER_API_KEY=... python scripts/ops/hatch_backfill_llm.py classify \\
                --input /tmp/hatch-candidates.csv \\
                --output-jsonl /tmp/hatch-decisions.jsonl \\
                --output-csv /tmp/hatch-decisions.csv
              python scripts/ops/hatch_backfill_llm.py write-apply-ids \\
                --input /tmp/hatch-decisions.jsonl --output /tmp/hatch-apply-session-ids.txt
            """
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export-hosted", help="Export hosted one-shot candidates to CSV.")
    export.add_argument("--ssh-host", default="zerg")
    export.add_argument("--subdomain", default="david010")
    export.add_argument("--output", default="/tmp/longhouse-hatch-backfill-candidates.csv")
    export.add_argument(
        "--include-zero-user",
        action="store_true",
        help="Also export zero-user rows. Default is exactly one user message.",
    )
    export.set_defaults(func=export_hosted)

    classify_parser = sub.add_parser("classify", help="Classify exported candidates through OpenRouter.")
    classify_parser.add_argument("--input", required=True)
    classify_parser.add_argument("--output-jsonl", default="/tmp/longhouse-hatch-backfill-decisions.jsonl")
    classify_parser.add_argument("--output-csv", default="/tmp/longhouse-hatch-backfill-decisions.csv")
    classify_parser.add_argument("--model", default=MODEL)
    classify_parser.add_argument("--workers", type=int, default=6)
    classify_parser.add_argument("--batch-size", type=int, default=8)
    classify_parser.add_argument("--timeout", type=float, default=45.0)
    classify_parser.add_argument("--retries", type=int, default=2)
    classify_parser.add_argument("--limit", type=int, default=0)
    classify_parser.add_argument("--progress-every", type=int, default=25)
    classify_parser.add_argument("--no-resume", action="store_false", dest="resume")
    classify_parser.set_defaults(func=classify, resume=True)

    apply_ids = sub.add_parser("write-apply-ids", help="Write session IDs whose reviewed decision has apply=true.")
    apply_ids.add_argument("--input", required=True)
    apply_ids.add_argument("--output", default="/tmp/longhouse-hatch-backfill-apply-session-ids.txt")
    apply_ids.add_argument(
        "--classification",
        help="Optional classification filter; writes high/medium confidence IDs for that class.",
    )
    apply_ids.set_defaults(func=write_apply_ids)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
