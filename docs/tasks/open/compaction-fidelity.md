# Compaction Fidelity + Active Context Semantics

Status: In progress
Last updated: 2026-03-17

## Goal

Preserve full transcript fidelity while truthfully modeling what the model still “remembers” after `/compact`.

## Done when

- Forensic history stays lossless and queryable.
- Active-context projections stay truthful around compaction boundaries.
- The remaining compaction/noise decision is settled explicitly and covered by tests.

## Checklist

- [x] Persist compaction metadata as first-class events
- [x] Derive explicit compaction boundaries and expose `forensic` vs `active_context`
- [x] Surface pre-compaction rows honestly in UI/search instead of deleting them
- [x] Add retention/sync guardrails so archived source transcripts survive cleanup windows
- [ ] Decide whether high-volume `progress` records become hidden first-class events or remain intentionally dropped, and lock that choice down in code/tests

## Notes

- The active open item is small but important: do not leave `progress` handling in an ambiguous half-state.
