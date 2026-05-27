# Runtime Display Contract

The server emits a single presentational projection — `runtime_display` — that
web and iOS render without policy. This document describes that contract: what
each axis means, what joint states are valid, and where the contract lives.

If you are adding a new field, changing a legal value, or chasing a divergence
between web and iOS rendering, start here.

## Audience

- Server engineers touching `session_runtime_display.py`, `session_views.py`,
  `session_runtime.py`.
- Web engineers in `web/src/lib/sessionRuntime.ts`,
  `web/src/services/api/agents.ts`.
- iOS engineers in `ios/Sources/Shared/SessionModels.swift` and the generated
  `SessionAPI.generated.swift`.

The clients should not invent runtime/lifecycle policy. They take
`runtime_display` and render it.

## Non-goals

- Wire shape for raw observations (`SessionLivenessFacts`). That dataclass
  stays internal — capability gating only.
- Transcript content, message history, or tool payloads. `runtime_display` is
  about session-level "who's talking and is it live", not transcript data.

## Where the contract lives

- **Definition**: `SessionRuntimeDisplay` in
  `server/zerg/services/session_runtime_display.py`.
- **Wire shape**: `SessionRuntimeDisplayResponse` in
  `server/zerg/services/session_views.py`.
- **OpenAPI contract**: regenerated via `make generate-sdk`, propagated to
  `web/src/generated/openapi-types.ts` and
  `ios/Sources/Shared/Generated/SessionAPI.generated.swift`.
- **Tests**: snapshots in `server/tests_lite/runtime_display_snapshots/`,
  invariants in `server/tests_lite/test_runtime_display_invariants.py`.

To change the contract: edit `SessionRuntimeDisplay`, then `make generate-sdk`,
then update affected snapshots, then update web/iOS render sites.

## The six axes

`runtime_display` has six independent axes plus copy fields and booleans. The
axes are independent in the sense that each describes a distinct property of
the session; valid joint states are constrained (see [Joint states](#joint-states)).

### `truth_tier` — confidence in the runtime signal

| value           | meaning                                                                  |
| --------------- | ------------------------------------------------------------------------ |
| `managed-local` | Longhouse owns the control path **and** has a fresh runtime signal.      |
| `fresh`         | Fresh runtime signal but no managed control path (e.g. imported and live). |
| `stale`         | Had a signal once, current state is uncertain.                           |
| `none`          | Never observed runtime activity for this session.                        |

### `signal_tier` — how the runtime signal was sourced

| value                      | meaning                                                                |
| -------------------------- | ---------------------------------------------------------------------- |
| `process_binding`          | Inferred from the host machine binding (process online/gone).          |
| `phase_signal`             | Phase-level event without binding confirmation.                        |
| `transcript_progress`      | Inferred only from new transcript events.                              |
| `none`                     | No signal source.                                                      |

### `state` — current presence (`presence_state` on the wire is `state`)

| value                | meaning                                              |
| -------------------- | ---------------------------------------------------- |
| `thinking`           | Provider is processing.                              |
| `running`            | Tool is running.                                     |
| `idle`               | Provider is idle within an active turn.              |
| `needs_user`         | Provider is waiting for a user prompt.               |
| `blocked`            | Provider is awaiting permission/approval.            |
| `stalled`            | Provider reported stalled.                           |
| `syncing_transcript` | Managed session post-turn, transcript still arriving. |
| `null`               | No presence; explicit absence.                       |

### `control_path` — does Longhouse own the steering channel?

| value       | meaning                                                                       |
| ----------- | ----------------------------------------------------------------------------- |
| `managed`   | At least one of `live_control_available` or `host_reattach_available` is true. |
| `unmanaged` | Neither is available; the session is observe-only.                            |

### `activity_recency` — how recently we heard *something*

| value    | meaning                                                            |
| -------- | ------------------------------------------------------------------ |
| `live`   | Presence is live and within its phase freshness window.            |
| `recent` | Recent activity but no live phase signal (reserved; see Note).     |
| `stale`  | Had signal previously, nothing fresh now.                          |
| `none`   | Never observed activity.                                           |

> Note: `recent` is currently only emitted as a future hook; today the reducer
> emits `live` / `stale` / `none`.

### `lifecycle` — open / closed / unknown

| value     | meaning                                                                                       |
| --------- | --------------------------------------------------------------------------------------------- |
| `open`    | Session is open; new events are still possible.                                               |
| `closed`  | Session is irreversibly closed (`session_ended`, `user_closed`, or `process_gone`).            |
| `unknown` | Terminal signal is reversible or unverified (`host_expired`, `finished`, etc.).               |

## Other fields

- `host_state` — `online | stale | offline | unknown`. Reflects the machine
  binding, independent of `control_path`.
- `terminal_reason` — populated when `lifecycle == "closed"`. One of
  `session_ended | user_closed | process_gone | host_expired | provider_signal`,
  or `null`.
- `tone` — drives client color choice. `stalled | blocked | running | thinking | idle | active | inactive | closed`.
- `headline`, `detail`, `phase_label`, `compact_tool_label` — copy emitted by
  the server. Clients must render these strings as-is. No client-side
  copy canonicalization, no fallbacks.
- Booleans — `is_live`, `is_executing`, `needs_attention`, `is_idle`,
  `is_stalled`, `is_managed_local_truth`, `has_signal`. These must be
  derivable from the axes above; they're emitted for client convenience.

## Joint states

These constraints are asserted as invariants in
`test_runtime_display_invariants.py`. Adding a value to an axis means
restating these invariants.

- `is_live == is_executing` always. (They mean the same thing.)
- `state in {"running", "thinking"}` ⇒ `is_executing == True`.
- `is_stalled == True` ⇔ `state == "stalled"` ∧ `tone == "stalled"`.
- `needs_attention == True` ⇒ `state == "blocked"` ∧ `lifecycle != "closed"`.
- `truth_tier == "managed-local"` ⇒ `control_path == "managed"`
  ∧ `is_managed_local_truth == True`.
- `lifecycle == "closed"` ⇒ all of:
  `is_executing == False`, `needs_attention == False`,
  `is_idle == True`, `headline == "Closed"`,
  `phase_label == "Closed"`, `tone == "closed"`,
  `state == None`.
- `lifecycle == "closed"` ⇒ `terminal_reason is not None`.
- `has_signal == False` ⇒ `state == None`
  ∧ `truth_tier in {"stale", "none"}`
  ∧ `activity_recency in {"stale", "none"}`.
- `state == "syncing_transcript"` ⇒ `is_idle == False` ∧ `is_executing == False`.

Aspirationally true (currently violated by the reducer in edge cases — fix
before promoting to invariant):

- `host_state == "offline"` ⇒ `activity_recency != "live"`. The reducer can
  emit `host_state=offline` + a fresh phase signal that survives the offline
  binding. Either suppress the live presence when host goes offline, or
  weaken to "host_state==offline ∧ presence within phase_freshness window
  may be `live`, otherwise must not be".

## Worked example — managed orphan bridge

A session whose Codex bridge is online but the last transcript event is 5
minutes old.

Inputs:

- `capabilities.live_control_available = True`
- `capabilities.host_reattach_available = True`
- `runtime_view.confidence = "live"`
- `runtime_view.presence_state = "running"`
- `runtime_view.runtime_source = "codex_bridge"`
- `binding_host_state = "online"`
- `binding_terminal_reason = None`
- last `phase_signal` 5min ago, within `PHASE_FRESHNESS["running"]` (10min)

Expected projection:

```jsonc
{
  "truth_tier": "managed-local",
  "signal_tier": "phase_signal",
  "state": "running",
  "control_path": "managed",
  "activity_recency": "live",
  "lifecycle": "open",
  "host_state": "online",
  "terminal_reason": null,
  "is_live": true,
  "is_executing": true,
  "is_idle": false,
  "is_stalled": false,
  "is_managed_local_truth": true,
  "has_signal": true,
  "needs_attention": false,
  "tone": "running",
  "headline": "Working",
  "detail": "Using <tool>",
  "phase_label": "Using <tool>"
}
```

Two minutes later, the bridge stops emitting and `phase_signal` ages past 10
min: `confidence` flips to `stale`, `state` becomes `null`, `truth_tier`
falls to `stale`, `activity_recency = "stale"`, `tone = "inactive"`,
`lifecycle` stays `open`, `host_state` stays `online`. No client policy
needed.

## Updating a snapshot

```bash
cd server
UPDATE_RUNTIME_DISPLAY_SNAPSHOTS=1 uv run pytest tests_lite/test_runtime_display_snapshots.py
```

The snapshot test parametrizes across every JSON in
`server/tests_lite/runtime_display_snapshots/`. Each file is a `.json` with
`input` (the `SessionRuntimeView` + capabilities + binding fields needed by
`build_session_runtime_display`) and `expected_runtime_display` (the full
projection).

When updating: read the diff. The whole point is that a change to the
projection touches one file per scenario. If you find yourself running
`UPDATE_…=1` mechanically, you are bypassing the contract; stop and explain
why the new shape is correct.

## Reading a divergence

If web and iOS render different things for the same session:

1. Inspect the wire payload — is `runtime_display` the same on both sides?
   If not, it's a fetch/cache/state issue, not a contract issue.
2. Check the snapshot fixture for the closest matching scenario. Is there
   one? If not, add it.
3. Confirm both clients consume only `runtime_display` for the disputed
   field. If either has a fallback or canonicalization, that's the bug —
   delete the fallback, never replicate it.

## Out-of-scope but related

- Transcript-row state (e.g. dropped tool calls): tracked separately, not
  part of `runtime_display`. Server-authoritative `tool_call_state`
  (`running` | `completed` | `dropped`) lives on each assistant tool
  event; clients consume but never re-derive.
- Provider/binding observation tree: internal to the server, not on the
  wire.
