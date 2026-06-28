# Antigravity tool_call_id backfill (historical repair)

## Problem

The forward fix (`6c0d260b9`, parser adjacency-queue) pairs antigravity tool
results on NEW ingest. Historical rows ingested before it are still broken:
on david010, 4145 antigravity tool calls / 4111 tool results, **all 4111 with
`tool_call_id = NULL`, zero paired**, across **38 sessions** (all 38 still
retain their `source_lines`).

The existing `tool_result_repair.py` job does NOT fix this:
- It is INSERT-based (built for the Bedrock "missing role=tool row" case). The
  antigravity result rows already EXIST; they just lack a `tool_call_id`. The
  right operation is an in-place UPDATE, not an insert.
- It re-derives via the Python shipper parser, which has zero antigravity
  awareness, and parses single lines in isolation (no cross-record look-back).

## Shape of the fix

A new, separate repair path that backfills `tool_call_id` on existing
antigravity `role=tool` rows by re-deriving the adjacency pairing from the
session's archived `source_lines`.

### Re-derivation: reuse the shipped Rust parser (single source of truth)

Do NOT re-implement the adjacency-queue pairing in Python — it would drift from
the Rust source of truth in `engine/src/pipeline/parser.rs`. Instead:

1. For each affected session, reassemble its antigravity transcript bytes from
   `source_lines` (ordered by `source_offset`, one `(source_path, offset)` per
   line, decoded via the existing `raw_json` / `raw_json_z` codec path), writing
   to a temp file.
2. Run the engine `parse --dump-events` over that temp file (the exact shipped
   pairing logic), producing events with `source_offset` and `tool_call_id`.
3. Build a map `source_offset -> tool_call_id` for `role=tool` events.
4. For each existing durable antigravity `role=tool` row with `tool_call_id IS
   NULL` whose `source_offset` is in the map, UPDATE its `tool_call_id` to the
   derived value.

Match rows to derived ids by `(session_id, branch_id, source_offset)` — the
stable identity of a result row. Never touch a row whose derived id is None.

### Guards (mirror tool_result_repair.py discipline)

- Dry-run default; `--apply` required to write.
- Scope to `provider == "antigravity"`, `role == "tool"`, `event_origin ==
  "durable"`, `tool_call_id IS NULL`.
- Per-session isolation: a parse/exception on one session skips it, does not
  abort the batch. Count skipped.
- Idempotent: re-running after apply finds nothing (rows already have ids).
- Bounded: `--limit` sessions, `--session-id` single-session targeting.
- Report: sessions scanned, rows updated, rows left null (no derived id),
  sessions skipped.
- Caller owns commit (CLI wraps in a transaction, commits only on `--apply`).

### Why UPDATE is safe here (vs the event_hash concern)

The Codex pre-merge note on the forward fix said replaying a result yields a
different `event_hash` than the old NULL-id row, so an INSERT path would
duplicate. This repair sidesteps that entirely: it UPDATEs the existing row's
`tool_call_id` in place. We deliberately do NOT recompute `event_hash` — the
row keeps its identity; only the previously-null correlation column is filled.
(If a downstream consumer requires event_hash to include tool_call_id, that is
a separate migration; the dedup index does not depend on it.)

## Where it runs

The CLI command ships in the runtime image. Execute against the hosted DB via
`docker exec` on the runtime container (david010), where `source_lines` and the
result rows live in the same SQLite DB. Dry-run there first, review counts,
then `--apply`.

## Out of scope

- The other two backward jobs (inline-media backfill, stale-project) are
  independent and handled separately.
- Genuinely empty antigravity tool results (return-before-emit in the parser)
  remain unpaired; not addressed here.

## Tests

- Unit: a session whose source_lines reassemble to planner→result pairs →
  repair fills the expected `antigravity-{step}-{idx}` ids on the NULL rows.
- Dry-run does not mutate.
- Idempotent re-run updates nothing.
- A result row with no derived id (orphan result) stays NULL.
- Non-antigravity providers are never touched.
