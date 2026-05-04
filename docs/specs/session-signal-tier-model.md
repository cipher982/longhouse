# Session Signal Tier Model

Status: Implemented / keep in sync
Last updated: 2026-05-04
Related:
- `session-runtime-display-contract.md`
- `session-liveness-honesty.md`
- `session-state-surface-convergence.md`

## Summary

Session state must be derived from the kind of truth Longhouse actually has.
The Runtime Host used to let transcript progress, semantic phase signals, and
machine binding observations collapse into the same runtime phase shape. That
is why generic imported sessions can look `Running`, and why a provider prompt
can look like a durable `Needs you` state hours later.

This spec splits the flow into three layers:

1. **Source signals**: provider/runtime facts we observed.
2. **Runtime reduction**: durable DB state about phase, activity, and terminal
   signals.
3. **Display contract**: `runtime_display`, consumed verbatim by web and iOS.

Clients may format layout locally, but they must not re-derive the meaning of a
session from raw fields when `runtime_display` exists.

## Signal Tiers

| Tier | Meaning | Examples | May set live phase? |
| --- | --- | --- | --- |
| `phase_signal` | Longhouse received a semantic phase signal from a managed control path or runtime hook. | `longhouse codex`, `longhouse claude`, bridge/hook phase events. | Yes |
| `process_binding` | Machine Agent observed a bare provider process or confirmed it disappeared. | Bare CLI binding, process-gone observation. | No |
| `transcript_progress` | A transcript or imported session appended content. | `agents_ingest`, generic `opencode`/Sauron-email imports. | No |
| `none` | No usable runtime signal. | Cold imported or incomplete session. | No |

Important rule: `progress_signal` updates activity timestamps; it must not
invent `phase=running`. Only `phase_signal` and `terminal_signal` own semantic
phase/lifecycle changes.

## Display Matrix

| Control path | Signal tier | Raw phase / recency | Lifecycle | UI label | Tone | Action surface |
| --- | --- | --- | --- | --- | --- | --- |
| managed | phase_signal | `thinking` / `running`, fresh | open | `Working` or `Running <tool>` | running | Send / steer / queue |
| managed | phase_signal | `blocked`, fresh | open | `Needs permission` or `Blocked on <tool>` | blocked | Approve / send / queue |
| managed | phase_signal | `needs_user`, fresh | open | `Ready` | idle | Send |
| managed | phase_signal | `idle`, fresh | open | `Ready` | idle | Send |
| managed | phase_signal | `thinking` / `running`, stale | open | `Stalled` | stalled | Reattach |
| managed | none | no fresh bridge truth | open | `Disconnected` | inactive | Reattach or control offline |
| managed | any | explicit terminal signal | closed | `Closed` | inactive | Read-only |
| unmanaged | process_binding | online process visible | open | `Active` | active | Read-only |
| unmanaged | process_binding | process gone / host expired | closed | `Closed` | inactive | Read-only |
| unmanaged | transcript_progress | last progress within window | open | `Recent activity` | inferred | Read-only |
| unmanaged | transcript_progress | stale progress | open | `Inactive` | inactive | Read-only |
| unmanaged | transcript_progress | explicit terminal signal | closed | `Closed` | inactive | Read-only |
| unmanaged | none | no useful signal | unknown | `Unknown` | inactive | Read-only |

`needs_user` remains a raw semantic phase for debugging, delivery, and future
queueing decisions. It is not an attention state in timeline UI. `blocked` is
the only attention phase.

## Metadata Policy

Git metadata is independent of runtime state.

- `git_branch` is shown only when the source supplied a real branch.
- Missing branch is honest; the UI should omit the branch chip.
- `HEAD` is a noisy pseudo-branch and should not be promoted as a friendly
  branch label.
- Branch capture for generic imported providers belongs in the Machine Agent
  ingest path, not in web or iOS.

## Implementation Plan

### Phase 1 - Reducer Fidelity

Tests first in `server/tests_lite/test_session_runtime.py`:

- progress-only ingest never sets phase to `running`
- progress after a semantic phase signal preserves existing phase truth while fresh
- progress after stale phase truth does not revive phase truth
- `needs_user` freshness is not extended by progress-only activity
- opencode-like transcript ingest renders as recent/inactive, not running

Implementation:

- Stop mutating `SessionRuntimeState.phase` in the `progress_signal` branch.
- Keep `last_progress_at`, `last_live_at`, and `timeline_anchor_at`.
- `build_runtime_view` should expose phase only when phase truth came from a
  semantic source and is fresh enough for the display contract.

### Phase 2 - Explicit Signal Tier

Add `signal_tier` to the backend runtime/display projection. Keep it optional
for clients at first.

Tests:

- managed live phase -> `phase_signal`
- unmanaged online binding -> `process_binding`
- progress-only ingest -> `transcript_progress`
- cold session -> `none`

### Phase 3 - Display Projection

Refactor `server/zerg/services/session_runtime_display.py` to project from
`control_path`, `signal_tier`, lifecycle, and raw phase. The output fields
remain the shared UI contract:

- `state`
- `tone`
- `headline`
- `detail`
- `phase_label`
- `needs_attention`
- `activity_recency`
- `lifecycle`
- `host_state`

Display policy:

- `needs_user` -> `Ready`, `idle`, `needs_attention=false`
- `blocked` -> attention tone and copy
- transcript progress -> `Recent activity` or `Inactive`
- closed only from terminal/process-gone truth

### Phase 4 - Web Consumption

Web timeline/detail should render `runtime_display` and `timeline_card`
directly. Local derived helpers are last-resort guardrails for incomplete dev
fixtures, not an alternate truth model for supported API responses.

### Phase 5 - iOS Consumption

iOS timeline/detail/widget/live activity should render the same
`runtime_display` fields. Native components can keep native layout, but not a
separate runtime state machine.

## Definition of Done

- The matrix above is covered by backend tests.
- Web and iOS tests assert the same labels/tones from the same contract.
- Generic imported providers cannot render as `Running` without semantic phase
  truth.
- `Needs you` no longer appears as a timeline/card state.
- Missing branch metadata omits branch UI instead of inventing a fallback.
