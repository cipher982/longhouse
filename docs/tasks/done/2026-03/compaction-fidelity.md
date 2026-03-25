# Compaction Fidelity + Active Context Semantics

Status: Complete
Last updated: 2026-03-25

## Goal

Preserve full transcript fidelity while truthfully modeling what the model still "remembers" after `/compact`.

## Done when

- Forensic history stays lossless and queryable.
- Active-context projections stay truthful around compaction boundaries.
- The remaining compaction/noise decision is settled explicitly and covered by tests.

## Checklist

- [x] Persist compaction metadata as first-class events
- [x] Derive explicit compaction boundaries and expose `forensic` vs `active_context`
- [x] Surface pre-compaction rows honestly in UI/search instead of deleting them
- [x] Add retention/sync guardrails so archived source transcripts survive cleanup windows
- [x] Decide whether high-volume `progress` records become hidden first-class events or remain intentionally dropped, and lock that choice down in code/tests

## Decision: Progress Events (2026-03-25)

**Choice: intentionally dropped.** Progress events are high-volume hook/tool noise (hundreds per session). They are preserved losslessly in the source archive but excluded from parsed events. This avoids timeline noise without losing bytes. Locked down with a dedicated parser test (`test_progress_events_are_intentionally_dropped`) and code comments in both `parse_session_file` and `parse_session_file_full`.
