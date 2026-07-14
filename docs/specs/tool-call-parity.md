# Tool Call Storage & Presentation Parity

Status: Active
Last updated: 2026-07-13
Owner: Longhouse

## Summary

Provider TUIs collapse many ordinary tool calls into one line
("Searched for 3 patterns, read 9 files"). Those strings are **not**
transcript events. Longhouse keeps every call and result as a discrete
durable row, pairs them by `tool_call_id`, and derives an
**exploration run** summary at read time.

This epic does **not** claim exact provider parallelism. Same-assistant
emission batches (Claude `content[]`, Cursor multi `tool-call` blocks) are
wire facts; concurrent execution is not something Longhouse reconstructs.

## Vocabulary

| Term | Meaning |
|------|---------|
| **Canonical call** | `role=assistant` + `tool_name` (+ `tool_input_json`, `tool_call_id`) |
| **Canonical result** | `role=tool` + `tool_output_text` (+ `tool_call_id`) |
| **Pairing** | Match result → call by `tool_call_id`; FIFO only for legacy missing IDs |
| **Display tier** | `noise` / `context` / `action` — how a singleton renders |
| **Aggregate** | `search` / `read` / `list` / null — whether a completed call may join an exploration run |
| **Exploration run** | Maximal consecutive run of completed summary-eligible calls after pairing |

Do **not** persist a `tool_batch_id` for UI. Do **not** scrape TUI summary strings.

## Presentation contract (shipped)

1. Singleton eligible calls (e.g. one `Read`) stay individual one-liners.
2. Runs of **2+** completed `aggregate`-eligible calls collapse to
   `Searched N · Read M · Listed K` (omit zeros; counts = calls).
3. `WebFetch` / `WebSearch` never join (provenance).
4. Action-tier tools, user messages, assistant prose, seams, orphans,
   pending/running/dropped calls always break a run.
5. Expand shows the latest 8 calls plus `Show N earlier` (never permanent hide).
6. Envelope identity is irrelevant to presentation.

Config: `config/tool-tiers.json` (`tier` + `aggregate`). Shared fixtures:
`tests/fixtures/session-projection/`.

## Pairing contract (lockstep)

- `server/zerg/services/session_views.py` → `build_tool_call_state_map`
- `web/src/lib/sessionWorkspace/timelineModel.ts` → `buildTimelineModel`
- `ios/Sources/Shared/TimelineBuilder.swift` → `TimelineBuilder.build`

## Raw archive

Grouped rows are disposable projections. Reconstruct provider state from
`source_lines` / `raw_json` where present; do not treat flat timeline rows
as the sole archive.

## Related

- `config/tool-tiers.json`
- `docs/specs/cursor-transcript-format.md`
- `server/zerg/services/tool_result_repair.py`
