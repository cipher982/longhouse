# Launch Runtime Simplification

Status: In progress

## Goal

Make the runtime story honest and easy to explain before launch:

- Longhouse is the session kernel and integrated UI for CLI sessions.
- Oikos is the coordinator inside Longhouse.
- Cloud work is managed CLI sessions in Longhouse workspaces.
- Free local install/demo is the primary proof-of-value path before hosted billing or provisioning friction.
- `commis` can stay as an internal implementation term, but not as the main user-facing noun.

## Non-Goals

This pass does **not**:

- touch the active `/api/agents/*` auth-boundary split
- do a broad internal rename of `Commis*` symbols
- remove the fiche data model from every internal subsystem
- rewrite or delete the remaining Oikos runtime harness
- optimize the hosted self-serve funnel before the free local wedge is crisp

## Scope

### Slice 1: Contract Truth

- Rewrite Oikos prompt/tool descriptions to stop describing custom autonomous agents running on servers.
- Codify one honest provider capability contract for:
  - archive visibility
  - cloud session start
  - direct web continuation
  - hooks support
  - telemetry quality
- Add guardrail tests so prompt/tool copy and provider truth do not drift again.

### Slice 2: Launch-Facing Copy

- Publish the same provider capability story in launch-facing docs/UI.
- Reframe launch-facing docs/UI around the session-kernel MVP instead of a broad dashboard/SaaS story.
- Lead with the free local wedge and demo path; position hosted as the always-on convenience upgrade.
- Rename visible `commis` labels in Oikos/admin/operator surfaces to `cloud session` or `cloud job`.
- Keep internal symbol churn intentionally low.

### Slice 2b: Proof-of-Value Narrative

- Publish one canonical demo journey: recover context -> inspect the raw session -> coordinate -> continue from anywhere.
- Surface at least one machine-first example (`longhouse ...` or `/api/agents/*`) in launch-facing docs so the product does not read like "just a website."
- Make the current hosted-vs-free boundary explicit and honest.

### Slice 3: Non-Auth Oikos Model Cleanup

- Add compatibility `task_id` / `task_name` fields to Oikos run summaries.
- Move non-auth Oikos UI toward `task` wording instead of leaking `fiche` into user-facing surfaces.
- Keep backward-compatible `fiche_*` fields until a later deletion pass.

## Out of Scope but Adjacent

- Browser archive API split and `/api/agents/*` machine-only enforcement
- Narrowing `AUTH_DISABLED`
- Hosted control-plane ↔ tenant auth handoff cleanup
- Full fiche-platform deletion

## Acceptance Criteria

- Oikos prompt and spawn tool no longer describe cloud work as custom autonomous server agents.
- A single provider capability contract exists in code and is covered by tests.
- Launch-facing docs/UI describe provider support consistently with the real runtime and frame Longhouse as a session kernel first.
- The launch-facing docs clearly lead with the free local wedge and the hosted-beta upgrade path.
- One proof-of-value demo journey is documented and reusable across README, landing copy, and demo videos.
- Oikos run summaries expose `task_*` compatibility fields and the touched UI prefers them.
- `make test` and `make test-frontend-unit` pass on the final tree.

## Notes

- 2026-03-16: The primary risk is copy drift, not runtime mechanics. Prefer small truth passes over ambitious renames.
- 2026-03-16: Claude remains the strongest provider for direct continuation, hooks, and telemetry. Archive support is broader than runtime parity.
- 2026-04-02: Prelaunch should sell the session-kernel wedge, not the entire Longhouse endgame in one breath.
