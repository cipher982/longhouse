# Antigravity tool-call ↔ tool-result pairing

## Problem

On hosted `david010`, 100% of antigravity tool calls are orphaned (1537/1537 in the
Jun 25–26 window) — every antigravity `role=tool` event is ingested with
`tool_call_id = NULL`, so it can never pair with its assistant tool call. This is
distinct from the Bedrock/Claude empty-result fix (claude is now ~1.8%, codex ~0%).
The orphan makes every antigravity tool call render as dropped/running forever and
keeps the iOS dropped-vs-running guardrail load-bearing.

## Root cause

`engine/src/pipeline/parser.rs::extract_antigravity_events` emits two event kinds:

- **Tool call** — from a record carrying `tool_calls: [{name, args}]` (a
  `PLANNER_RESPONSE`). Emitted as `Role::Assistant` with
  `tool_call_id = Some("antigravity-{step_index}-{idx}")`.
- **Tool result** — from a `Role::Tool` record (a `MODEL`-source record whose
  `type` does not end in `_RESPONSE`, e.g. `GREP_SEARCH`, `VIEW_FILE`,
  `LIST_DIRECTORY`). Emitted with `tool_call_id: None` — **hardcoded**.

So calls get a synthetic id but results never do.

## Native format (confirmed against a real transcript)

`~/.gemini/antigravity-cli/brain/<session>/.system_generated/logs/transcript.jsonl`:

```
step=2  PLANNER_RESPONSE  src=MODEL  tool_calls=['grep_search']   <- the CALL
step=3  GREP_SEARCH       src=MODEL  tool_calls=None              <- the RESULT
step=5  PLANNER_RESPONSE  src=MODEL  tool_calls=['view_file']
step=6  VIEW_FILE         src=MODEL  tool_calls=None
...
```

Key facts from the sample (one ~3500-event session):
- Every planner carried exactly **one** tool_call (40/40). Multi-call must still be
  handled defensively.
- The result is the record **immediately following** a planner-with-tool_calls.
- Pairing is reliable by **adjacency**, NOT by tool name. 5/40 had a name alias
  mismatch (`list_dir` → `LIST_DIRECTORY`, `grep_search` → `GREP_SEARCH`) yet were
  correctly adjacent. Do not gate pairing on name equality.

## Fix

Thread a small look-back **queue** through the per-line parse loops so the
antigravity result branch inherits the pending planner call's `tool_call_id`.
(Design refined by adversarial review — see "Review outcomes" below.)

- State: `pending_antigravity_tool_calls: VecDeque<String>` (call_ids emitted by the
  most recent planner, in order), carried by `&mut` into `extract_events` →
  `extract_antigravity_events`.
- **Per source record (not per emitted event)**, after classifying the record:
  - Planner with `tool_calls`: emit each call as today with
    `antigravity-{step}-{idx}`, and **replace** the queue with those ids in order.
    (Replace, not append — a new planner supersedes any unconsumed prior call.)
  - `Role::Tool` result (`source == "MODEL"`, `type` not ending `_RESPONSE`):
    `pop_front()` the queue for this result's `tool_call_id`. Empty queue → `None`.
  - Any other valid antigravity record (`USER_INPUT`, `SYSTEM` content, assistant
    `_RESPONSE` content): **clear the queue** (fail-closed — an interleaving record
    means the planner's call had no adjacent result).
  - Blank/malformed lines (skipped by the loop before `extract_events`) do not touch
    the queue.
- **Guard:** only consume for a result whose `step_index == planner_step + 1` when
  both are present, reinforcing adjacency. Do not gate on tool name (the
  `list_dir`→`LIST_DIRECTORY` alias breaks name equality).
- A planner with no following result, or a result with no preceding planner, leaves
  `tool_call_id = None` — no false pairing.
- Both `parse_mmap` and `parse_buffered` loops own one queue each. `parse_gemini_json`
  (legacy `.json`) is out of scope unless it shows the same shape.

### Call-id form
Reuse the existing call-side id: `antigravity-{step_index}-{idx}`. The result
reproduces the **same string the call emitted** (carried in the queue), derived from
the *planner's* step_index, not the result record's.

### Incremental parse boundary (seed from prior record)
Offset resume (`parse_session_file(path, offset)`) is the NORMAL incremental path —
`transcript.jsonl` is flushed often, so planner-then-result routinely split across
batches. To avoid minting a fresh live orphan on every flush split: when
`offset > 0`, read the single complete JSONL record immediately before `offset`; if
it is an antigravity planner with `tool_calls`, seed the queue before the loop. This
is a cheap read, not persisted parser state.

## event_hash interaction (carried to backward-repair task #3)

`tool_call_id` participates in `event_hash` (`store.py` ~1075, mirrored in
`tool_result_repair.py` ~367). The DB unique index is
`(session_id, branch_id, source_path, source_offset, event_hash)`, NOT `tool_call_id`
— so two events sharing a tool_call_id (the intended pairing) is fine. BUT historical
replay/repair of an antigravity result now produces a non-NULL `event_hash` that
differs from the old NULL-id orphan row's hash, so the repair job must update/remove
the stale orphan rather than insert a duplicate. This is a constraint on task #3, not
this fix.

## Review outcomes (Hatch Codex GPT-5.5, pre-impl gate, 2026-06-26)

Adopted (pre-impl): queue over last-wins (multi-call safety); fail-closed clearing on
interleaving records; per-source-record state update; step/source/type guard;
prior-record seeding across offset boundaries; event_hash note for #3.

Pre-merge gate (Hatch Codex GPT-5.5, on commit f51de954e) found three real
fail-closed leaks, all fixed before merge:
1. Records with no/empty content fell through without clearing pending → moved the
   pairing-state update out of the content block so every antigravity record updates
   it.
2. A step-mismatch tool result returned None but left the queue intact → now clears.
3. Pairing keyed on `Role::Tool` (the fallthrough role) could pop the queue for a
   non-MODEL content record → now keyed on explicit
   `is_tool_result = source == "MODEL" && !type.ends_with("_RESPONSE")`.
Also tightened the missing-step_index arm from permissive (`true`) to fail-closed
(`false`), safe because `is_antigravity_line` guarantees step_index is present.

Validated post-fix against 8 real production transcripts: every tool result paired,
zero unpaired. 13 antigravity unit tests pass.

Residual (documented, not fixed): a multi-call planner split across an incremental
flush boundary after its first result loses the remaining queued calls (the seed
only restores from a planner record, not from mid-sequence). Rare; fails closed
(orphan, not mis-pair); recovered by the backward-repair job (#3).
Separate fragility (out of scope): genuinely empty antigravity tool results return
before emitting, so they still look unpaired — track if it appears in data.

## Tests

- New `parse_session_file` fixture: antigravity transcript with planner→result
  adjacency asserts the result event's `tool_call_id` equals the call's.
- Multi-call planner defensive case.
- Result-without-planner stays `None`.
- Name-alias case (`list_dir`/`LIST_DIRECTORY`) still pairs.
- `make test-engine`.

## Verification after merge

Re-run the david010 orphan-rate-by-provider query; antigravity should drop from
~100% toward the claude/codex floor on **new** ingest. Historical rows need the
backward-repair job (#3), which is gated on this fix.
