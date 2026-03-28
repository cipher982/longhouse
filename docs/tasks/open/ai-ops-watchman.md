# AI Ops Watchman

Status: In progress
Spec: `docs/specs/ai-ops-watchman.md`
Last updated: 2026-03-28

## Goal

Ship an AI-first operational watchman for Longhouse that watches raw tenant-local evidence, asks Grok 4.1 whether the recent story looks abnormal, and escalates with durable incidents plus email only when it has concrete evidence.

## Done when

- The watchman has a formal spec and active rollout plan.
- A real Grok 4.1 smoke script succeeds and records input-token usage.
- The app persists raw watchman observations and watchman analysis runs.
- A builtin scheduled job can analyze recent observations and create/update `OperationalIncident` rows.
- Alert-worthy watchman analyses can send operator email with evidence.

## Checklist

- [x] Write the principles-first spec and rollout plan
- [x] Add a real-call Grok 4.1 smoke script for the watchman prompt path
- [x] Add watchman observation + run persistence
- [x] Add the analyzer service with structured JSON result parsing
- [x] Wire incidents + SES email escalation
- [x] Register the builtin watchman job
- [x] Add focused tests and verification
- [ ] Ship and verify on a live instance

## Notes

- Keep the observation schema intentionally thin.
- Input-token accounting is a hard requirement, not a nice-to-have.
- V1 is tenant-local and read-only; host-wide and auto-remediation work can follow later.
- Real direct-xAI smoke succeeded on `2026-03-28` against `grok-4-1-fast-reasoning` via `https://api.x.ai/v1`.
- Smoke usage sample: `641` input tokens, `170` output tokens, `651` reasoning tokens, estimated cost `0.0002132` USD.
- Integrated `run_watchman_cycle()` real-call smoke also succeeded on `2026-03-28` against a temp SQLite DB.
- Integrated usage sample: `1613` input tokens, `normal` result, estimated cost `0.0003681` USD.
