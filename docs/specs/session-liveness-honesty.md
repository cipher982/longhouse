# Session Liveness Honesty

Status: Reviewed, building
Last updated: 2026-04-27
Owner: maintainer
Related: the session runtime display contract design (internal spec), the managed Codex liveness design (internal spec), the machine-local managed session state design (internal spec)
Review: Internal review incorporated 2026-04-27 — see "Review findings" section below.

## One-sentence summary

Timeline cards confidently label unmanaged sessions "CLOSED" while the underlying CLI process is still running and emitting messages, because we conflated three independent axes — control path, signal recency, and lifecycle — into one overloaded `ended_at` field.

## Part 1 — Bug findings

### Observed behavior

On `tenant.example.com/timeline`, multiple unmanaged Codex session cards (`imgt`, `bar`) render a bold "CLOSED" pill with a "just now" / "1m ago" timestamp. The underlying `codex` TUI processes on the laptop are alive, attached, and still producing turns. A message landed ~30s before the screenshot; the card still said CLOSED.

### Root cause: `ended_at` means two different things

Two different producers write `ended_at` with incompatible semantics:

1. **Rust engine (`engine/src/pipeline/parser.rs:711-714`)**
   `metadata.ended_at = max(event.timestamp)` — i.e. "timestamp of the last line in the JSONL." Every ingest writes a fresh value. This is activity, not termination.

2. **Real termination** (explicit terminal signals from managed bridges, `/v1/sessions/close` from the provider) would be a genuine closure marker — but no such signal exists for unmanaged runs.

The backend (`server/zerg/services/agents/store.py:1358`) accepts #1 and writes it straight into `session.ended_at`. The frontend (`web/src/components/sessions/SessionCard.tsx:120-231`) then reads:

```
hasKnownClosedProcess = !!session.ended_at || runtime.status === "completed" || !!session.terminal_state
isClosedSession       = hasKnownClosedProcess && !hasCurrentControlledPresence
```

Because unmanaged sessions never have `hasCurrentControlledPresence`, **any set `ended_at` collapses to CLOSED**. The pill updates roughly inversely to session activity: the more the user uses the session, the more confidently we say it's dead.

### Secondary issues surfaced by the investigation

- **No "unknown" state.** Every unmanaged session is forced into Live / Recent / Idle / Closed. There is no honest "we stopped hearing but don't know why."
- **Process liveness is never consulted for unmanaged sessions**, even though `local-health` already does this for managed bridges.
- **"Started in Longhouse" vs "managed" drift** — UI copy conflates provenance with control path (already noted in `CLAUDE.md`, still not fully untangled in card surfaces).
- **`runtime_display` contract exists but doesn't cover unmanaged lifecycle** — it gives us presentation-ready state for managed sessions and falls back to heuristics for everything else without flagging the honesty gap.

## Part 2 — Axes we've been mixing

Today's card state is really three independent things we collapsed into one badge. The redesign is built on keeping them orthogonal.

| Axis | Values | Ground-truth source |
|---|---|---|
| **Control path** | `managed` / `unmanaged` | `session.capabilities.host_reattach_available` / `live_control_available` |
| **Signal recency** | `live` / `recent` / `stale` / `none` | Most recent phase or activity signal vs. now |
| **Lifecycle** | `active` / `idle` / `unknown` / `closed` | Explicit terminal signal or confirmed process death |

A card renders the combination. "CLOSED" is a lifecycle claim, and we should make it only when we actually know. "Live on cinder" is a (control + recency) claim. "Idle" is a recency claim. Treating them as one status string is what got us here.

## Part 3 — Redesign

### Design principles

1. **CLOSED is a promise.** Render it only with ground truth: explicit terminal signal, OR confirmed process-gone from local-health, OR (far-future) an unmanaged session whose host machine has reported the process dead.
2. **Activity timestamps are not lifecycle.** Stop writing `session.ended_at` from `max(event.timestamp)`. Introduce a separate `last_activity_at` for that.
3. **Unmanaged is a first-class state, not a degraded managed.** The pill and copy should name it honestly and describe what we can and cannot do with it.
4. **No silent heuristics.** If a state is a guess, the label says so ("Idle", "Stale — last seen 2h ago"). No confident CLOSED from age.
5. **Pre-launch, we can break storage.** No migrations for backwards compatibility. Drop the old column and redefine it.

### New state model

Backend materializes three fields on every session response:

```json
{
  "control_path": "managed" | "unmanaged",
  "recency":      "live" | "recent" | "stale" | "none",
  "lifecycle":    "active" | "idle" | "unknown" | "closed"
}
```

Promotion rules to `lifecycle = closed`:

- explicit `phase_signal{kind=terminal_signal, terminal_state=session_ended}` received, OR
- managed bridge reports process gone (via `local-health`), OR
- unmanaged + machine agent observes the pid/cwd is gone AND JSONL hasn't grown for > configurable window (default 1h), AND machine is online at the time of check.

Everything else is `lifecycle ∈ {active, idle, unknown}`:

- `active` — live phase signal (managed) OR JSONL activity within 5 min (unmanaged).
- `idle` — no live signal, activity in last 1h.
- `unknown` — no live signal, last activity > 1h, no ground-truth closure.

### New storage shape

Rename and split:

- `session.last_activity_at` (replaces the old write path for `ended_at`)
- `session.terminal_at` (only set when we have a real terminal signal)
- `session.terminal_reason` (enum: `provider_signal`, `process_gone`, `host_reported`, etc.)

Pre-launch: drop the old `ended_at` column outright, wire the engine and ingest to the new fields.

### New card copy

| Scenario | Pill | Subtext |
|---|---|---|
| Managed, live phase | `Live on cinder` | `Thinking` / `Running Shell` / etc. |
| Managed, recent but no phase | `Managed · idle` | `Last seen 3m ago` |
| Managed, terminal signal | `Closed` | `Ended 15m ago` |
| Unmanaged, active | `Unmanaged · active` | `Last activity just now` |
| Unmanaged, idle | `Unmanaged · idle` | `Last activity 12m ago` |
| Unmanaged, unknown | `Unmanaged · unknown` | `No activity for 3h — may still be running` |
| Unmanaged, process confirmed gone | `Closed` | `Process ended 2h ago` |

Managed-local truth tier keeps its existing rich labels (`runtime_display.detail`, tool name, etc.).

### Where this lives

- **Backend**: `session_runtime_display.py` owns the three-axis projection. It reads the new fields plus runtime overlay plus local-health bridge scans and emits `runtime_display` extended with `control_path`, `recency`, `lifecycle`.
- **Rust engine**: stops writing `ended_at`; writes `last_activity_at`. Emits `terminal_signal` only when it sees a real terminal event in JSONL (or, for managed, from the bridge).
- **Frontend**: `SessionCard.tsx` reads `runtime_display.lifecycle` not `session.ended_at`. Removes the `hasKnownClosedProcess` heuristic entirely.
- **iOS**: same — consume `runtime_display`, drop any client-side "ended_at means closed" logic.

### Non-goals (explicit)

- We do not attempt per-turn phase accuracy for unmanaged sessions. No hook injection into bare CLI.
- We do not chase managed-quality truth for unmanaged. "Unknown" is an acceptable honest answer.
- We do not touch briefings or insights in this slice. Loop inbox and turn reviews are already removed from the launch surface.

## Part 4 — Phased plan

### Phase 1 — Stop the lie (full web surface)

No UI path may render *closed*, *completed*, *dropped*, or *stop polling* based solely on parser-derived `ended_at` or fallback runtime `completed`/`finished`. Narrower scope than "frontend only" but wider than just the card.

Frontend changes:
- `web/src/components/sessions/SessionCard.tsx:120-231` — remove `hasKnownClosedProcess` dependency on `session.ended_at` and fallback `runtime.status === "completed"`. Render CLOSED only on explicit `terminal_state`.
- `web/src/lib/sessionRuntime.ts:224-231` + `web/src/lib/sessionUtils.tsx:234-255` — stop treating fallback `completed`/`finished` as terminal.
- `web/src/components/sessions/SessionRuntimeStrip.tsx:58-60` — unmanaged strip should not use `endedAt` for outcome label.
- `web/src/pages/SessionDetailPage.tsx:102, :245` — clock and tool-call terminal rendering must not close on `ended_at`.
- `web/src/hooks/useSessionWorkspace.ts:68-74, :170` — polling must not stop on `ended_at != null`.

Backend changes (minimum to unblock frontend):
- `server/zerg/services/session_runtime.py:273-321` — `build_fallback_runtime_view` must not set `status="completed"` or `terminal_state="finished"` from `ended_at` alone.
- `server/zerg/services/session_runtime_display.py:195-202` — do not promote to terminal on `ended_at`.

No storage change, no engine change. `ended_at` still holds parser-derived values; we just stop consuming them as terminal truth.

**Ship criteria:** `bar` and `imgt` cards on the hosted tenant do not render CLOSED while their `codex` processes are alive. Session detail pages keep polling for active unmanaged sessions.

### Phase 2 — Fix the data model

Split `ended_at` into `last_activity_at` + `terminal_at` + `terminal_reason`. Drop `ended_at`.

- Backend model change (`server/zerg/models/agents.py`) + `_migrate_agents_columns()`.
- Rust engine (`engine/src/pipeline/parser.rs`, `compressor.rs`) writes `last_activity_at`, never `ended_at`.
- Ingest (`agents/store.py`) routes fields to new columns.
- Timeline/session APIs expose new fields, drop `ended_at` from responses.
- Frontend + iOS consume new fields.

**Ship criteria:** No code reads or writes `ended_at`. Unit + ingest tests updated. Tombstone the old field in docs.

### Phase 3 — Three-axis runtime display

Extend `runtime_display` to carry `control_path`, `recency`, `lifecycle`. Backend computes them; clients render them verbatim.

- `session_runtime_display.py` gains the three-axis projection.
- Lifecycle = `closed` only on explicit terminal signal (today — process-gone check comes in Phase 4).
- Remove remaining frontend heuristics in `sessionRuntime.ts` and `SessionCard.tsx`.
- iOS `TimelineBuilder.swift` + session view consume the new fields.

**Ship criteria:** Web and iOS render identical lifecycle states for the same session. `runtime_display` is the single source of truth.

### Phase 4 — Process-gone truth for unmanaged

Use `local-health` to promote unmanaged sessions to `closed` when we have ground truth.

- Machine agent reports per-session pid/cwd liveness to the runtime host.
- Runtime overlay elevates `lifecycle=closed` with `terminal_reason=process_gone` when scan confirms.
- Tests: synthetic process-gone scenarios in integration harness.

**Ship criteria:** An unmanaged Codex session whose process is killed shows `Closed · Process ended Xm ago` within one scan cycle.

### Phase 5 — Copy, tone, QA pass

- Final pass on card copy across web, iOS widget, menu bar.
- QA with `ui-capture` across states.
- Delete all the old "ended_at = closed" assumptions and the frontend fallback derivations the contract allowed as compat code.

**Ship criteria:** Visual QA across timeline, session detail, iOS shelf, widget. No code path outside of display layer reads `last_activity_at` to infer closure.

## Review findings (internal review, 2026-04-27)

An internal review of the draft found several material gaps. Revisions folded into the phased plan above; key corrections:

- **Phase 1 scope was too narrow.** The `ended_at` lie propagates through `session_runtime.py:273-321` (fallback turns `ended_at` into `status="completed"` / `terminal_state="finished"`) and `session_runtime_display.py:195-202`. Gating only `SessionCard.tsx` on `session.ended_at` is insufficient — the same lie enters through runtime fallback. Phase 1 now covers the full web stop-the-lie surface: `SessionCard.tsx`, `SessionRuntimeStrip.tsx`, `SessionDetailPage.tsx`, `useSessionWorkspace.ts`, `sessionRuntime.ts`, `sessionUtils.tsx`. Backend fallback must stop emitting `completed`/`finished` from parser-derived `ended_at`.
- **Three-axis model had pollution.** `lifecycle = active / idle` is not lifecycle — it's recency/phase. Correct axes:
  - `control_path: managed | unmanaged` (durable from session capability, not live-ness)
  - `activity_recency: live | recent | stale | none`
  - `lifecycle: open | closed | unknown`
  - optional: `host_state: online | stale | offline | unknown`
  Managed phase (`thinking` / `running` / `idle` / `blocked`) stays in `runtime_display.state` where it lives today.
- **`last_activity_at` already exists** (`store.py:1532-1539`). Phase 2 is about making it canonical and deleting parser-derived `ended_at` writes, not introducing a new field.
- **Do not backfill `terminal_at` from old `ended_at`.** Old values are contaminated. Only set `terminal_at` from explicit `terminal_state=session_ended` rows going forward.
- **Phase 4 needs a session-binding prerequisite.** `local-health.unmanaged_processes` has no session_id today (`local_health.py:1536-1564`). Before process-gone truth can land, the Machine Agent must emit durable bindings: `machine_id`, `provider`, `provider_session_id`/source identity (path + inode), `pid`, `process_start_time`, `cwd`, `observed_at`, `source_offset/mtime`. Runtime Host consumes the Machine Agent heartbeat — not `local-health` directly.
- **Process-gone should be reversible.** If a session inferred `closed` via process-gone, later transcript growth should reopen it. Only explicit provider `session_ended` is final.
- **Host/machine health is a separate axis, not a `terminal_reason`.** Machine offline means "cannot verify," not closed. Add `host_state` to `runtime_display`; render as "Longhouse cannot verify."
- **Copy fixes.** Avoid "Unmanaged · active" — it sounds steerable. Use "Search only · recent activity", "Search only · stale", "Last activity Xh ago; Longhouse cannot verify whether it is still running." Keep "Live on cinder" only for fresh managed control truth. Managed but stale control path = "Control offline", not "Managed · idle".
- **iOS impact is larger than the draft showed.** `SessionViewModel.isSessionEnded` (`ios/Sources/LonghouseApp/SessionView.swift:1208-1215`) treats `status == completed` as terminal, affecting dropped/pending tool rendering. If the backend keeps emitting fallback `completed`, iOS keeps lying after web is fixed. New `runtime_display` fields must be added as optional in Swift `Codable` to avoid breaking older payloads.
- **Bridge `.sock` absence is not closed.** Managed detached/degraded is recoverable. Require an explicit terminal signal or multi-signal confirmation (bridge gone + provider child gone + bound session identity + no transcript growth).
- **Re-ordered sequencing:** do the full-web stop-the-lie first, then backend `runtime_display` three-axis extension, then client consumption (web + iOS), then storage cleanup, then machine-agent observations, and only then process-gone promotion.

Phase plan above now reflects these corrections.

## Part 5 — Open questions (answered post-review)

1. `host_offline` is **not** a `terminal_reason` — it's `host_state`. Label as "Longhouse cannot verify." Never closed.
2. 1h drives recency copy only, not lifecycle promotion. Lifecycle requires ground truth.
3. Expose raw `control_path` / `activity_recency` / `lifecycle` / `host_state` **and** server-formatted copy. Clients need raw for refresh cadence; must not re-derive semantics.
4. Keep "Live on cinder" for fresh managed control truth only.
5. Bridge `.sock` absence ≠ closed. Require explicit terminal signal or multi-signal confirmation.

1. Should `lifecycle=closed` with `terminal_reason=host_offline` exist, or do we punt that ("we don't know — host is offline")?
2. Threshold for `idle → unknown` — 1h feels right for Codex/Claude session cadence. Codex sessions routinely idle 30-60m between turns.
3. Do we expose `recency` raw to clients, or only through `tone`/`headline`? Leaning: expose it; clients already need it for refresh cadence.
4. Do we keep the "Live on cinder" phrasing? It's pre-existing and tested; changing it is scope creep.
5. Managed sessions that never emit a terminal signal but process clearly died — do we trust bridge `.sock` absence as ground truth, or require an additional check?
