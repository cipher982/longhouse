# Engine Shipper Byte Batching

Status: In progress
Last updated: 2026-03-17

## Goal

Make oversized engine shipper deltas make forward progress by splitting them into exact byte-range batches and replaying only the spooled range.

## Done when

- Oversized session deltas are split into deterministic exact byte batches.
- Replay uses the exact spooled byte window instead of a fuzzier approximation.
- Focused engine/shipper verification proves large sessions progress without data loss.

## Checklist

- [x] Land the correctness floor for exact replay behavior, partial EOF handling, and non-mutating dry-run behavior
- [ ] Split oversized deltas into exact byte-range batches
- [ ] Replay only the spooled byte range and prove forward progress on large sessions
- [ ] Add focused regression coverage for batch planning and exact replay
- [ ] Re-run engine/shipper verification on representative large fixtures or real sessions

## Notes

- This task is about correctness under size pressure, not general throughput tuning.
