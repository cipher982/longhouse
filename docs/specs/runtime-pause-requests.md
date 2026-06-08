# Runtime Pause Requests

Status: Draft implementation spec
Date: 2026-06-07
Owner: Runtime Host + Machine Agent + provider bridges
Related: `runtime-display-contract.md`, `managed-input-lifecycle.md`,
`session-alerting-research-spike.md`, `agents-machine-surface.md`,
`managed-provider-session-contract.md`

## Problem

Provider CLIs now pause mid-operation for user decisions that are neither normal
idle nor generic tool execution.

This spec is about structured clarifying/product questions, such as Claude
`AskUserQuestion`, Codex `requestUserInput`, and provider-specific elicitation
dialogs that can be rendered as questions.

This spec is not about routine tool permission prompts like "may I edit this
directory?" or "may I run this shell command?" Those are the existing
`blocked`/approval path. They are noisy and important for safety, but they are
not the product bug motivating this work.

Longhouse currently has only phase-level runtime truth:

```text
thinking | running | idle | needs_user | blocked
```

That vocabulary is useful but too coarse. `needs_user` currently means both:

- ordinary terminal-style handoff after a turn completed; and
- a mid-turn provider request that blocks the agent until the user answers.

The first case should be quiet. The second case should be visible and often
notifying. Today Longhouse intentionally renders `needs_user` as quiet idle, so
iOS can make a session look frozen even when the provider is waiting on a
multiple-choice user decision in the terminal.

`blocked` should continue to mean "permission required" unless a later explicit
cleanup changes that contract. Structured questions are not permission failures.
They are user decisions.

## Goal

Make provider-driven structured user questions first-class, durable, answerable
when the provider supports it, and consistently rendered by web/iOS.

The user should be able to look at Longhouse and answer:

1. Is the agent working, idle, or paused on me?
2. If paused on me, what kind of decision is needed?
3. Can I answer it from Longhouse, or do I need the terminal?
4. Has the pause resolved?

## Non-goals

- Do not scrape terminal pixels or synthesize keyboard input into TUIs.
- Do not make every `needs_user` phase an attention state.
- Do not use this project to improve routine tool permission approval UX.
- Do not collapse provider-specific structured-question protocols into free-text
  prompts.
- Do not silently auto-answer provider requests in normal managed sessions.
- Do not add a parallel phase writer outside the existing runtime phase reducer.
- Do not promise remote response for providers where Longhouse only has an
  observe-only hook surface.

## Core Decision

Add a durable provider-agnostic **pause request** layer for structured user
questions.

Do not add new top-level runtime phases for pause kinds. Keep phase as the
coarse execution state and attach an active pause request when the phase is
actionable.

```jsonc
{
  "phase": "needs_user",
  "pause_request": {
    "kind": "structured_question",
    "status": "pending",
    "provider": "codex",
    "can_respond": true,
    "questions": [...]
  }
}
```

Phase answers "what is execution doing?" Pause request answers "what exact
structured user question is blocking it?"

This separation matters because phase is freshness-windowed and inferred from
runtime signals, while a pending provider request must remain visible until it
is resolved, superseded, failed, expired, or the session closes.

## Vocabulary

### Phase

Existing phase values remain valid:

| Phase | Meaning |
| --- | --- |
| `thinking` | Provider is reasoning or preparing work. |
| `running` | Provider is executing a tool/action. |
| `idle` | Provider is idle or turn-complete. |
| `needs_user` | Provider is waiting for user input. This is quiet unless paired with an actionable pause request. |
| `blocked` | Provider is blocked on an approval/permission decision. |

### Pause Request Kind

V1 has one kind:

| Kind | Meaning | Typical phase |
| --- | --- | --- |
| `structured_question` | Provider needs one or more concrete answers. Claude `AskUserQuestion`, Codex `requestUserInput`, MCP elicitation, and equivalent provider dialogs all map here until a separate renderer is justified. | `needs_user` |

Ordinary turn handoff is not a pause request. Claude `idle_prompt` and similar
"ready for next prompt" notifications should remain phase-only `needs_user`.
Routine tool permission prompts remain phase `blocked` and stay outside this
v1 pause-request scope.

### Pause Request Status

| Status | Meaning |
| --- | --- |
| `pending` | Provider is still waiting, or Longhouse has not observed a resolution. |
| `resolved` | Provider accepted/continued, terminal user answered locally, or a later provider signal superseded the request. |
| `rejected` | User dismissed/rejected the structured question through Longhouse. |
| `failed` | Longhouse attempted to respond but provider/control delivery failed. |
| `expired` | Session/control path closed before a resolution arrived. |

`pending` is the only active attention status. The response route should wait
for bridge/control acknowledgement and return `resolved`, `rejected`, or
`failed`; do not persist a separate `responding` status unless implementation
proves asynchronous dispatch is unavoidable.

## Data Model

Add `SessionPauseRequest` on `AgentsBase`.

Minimum schema:

```text
id                         UUID primary key
session_id                 UUID indexed
runtime_key                string indexed
provider                   string
request_key                string unique
provider_request_id        string nullable
provider_ref_json          JSON/text nullable
kind                       string
status                     string
tool_name                  string nullable
title                      string nullable
summary                    string nullable
request_payload_json       JSON/text nullable
response_payload_json      JSON/text nullable
response_text              text nullable
can_respond                boolean default false
occurred_at                datetime
last_seen_at               datetime
resolved_at                datetime nullable
expires_at                 datetime nullable
created_at                 datetime
updated_at                 datetime
```

Indexes:

- `(session_id, status, occurred_at)`
- `(runtime_key, status, occurred_at)`
- unique `(request_key)`
- `(provider, provider_request_id)` when present

Do not add separate `provider_thread_id`, `provider_turn_id`,
`provider_item_id`, `response_schema_json`, or `response_route` fields in v1.
Keep provider-specific correlation details in `provider_ref_json` until a real
consumer needs a first-class column.

Do not store phase on the pause row. Phase remains owned by
`SessionRuntimeState`; pause kind is mapped to display semantics at read time.

`request_key` is Longhouse's dedupe key. It must be stable for retries and
specific enough to avoid collapsing two separate provider asks:

```text
<provider>:<session-or-runtime-key>:<provider-request-id-or-turn-item-kind>
```

Do not store secrets. Provider payloads may include paths, command text, or user
question text; keep the table private like other session data.

## Runtime Contract

Extend runtime ingest or provider adapters with pause events:

```text
pause_request
pause_resolution
```

Pause events are phase-read-only in v1:

- `pause_request` creates or refreshes a `SessionPauseRequest` and updates
  `last_seen_at`.
- `pause_resolution` resolves the matching active pause request.
- Phase remains written only by existing phase events, such as `phase_signal`.

Provider adapters that emit pause requests should also emit the corresponding
phase signal through the existing reducer:

- `structured_question` should accompany or follow `phase=needs_user`.

This preserves one writer per axis:

- `SessionRuntimeState.phase` is runtime execution truth.
- `SessionPauseRequest` is actionable decision truth.

Resolution rules:

- A provider-specific explicit resolution wins.
- A later `running` / `thinking` phase from the same provider/runtime resolves
  pending requests for that runtime unless the event payload says otherwise.
- A terminal signal (`session_ended`, `user_closed`, `process_gone`) expires
  pending requests for that session/runtime.
- A new pending request with a different `request_key` may supersede older
  pending requests for the same runtime when the provider only supports one
  active request at a time.

A pending pause request must not disappear solely because the phase freshness
window elapsed.

## Display Contract

Extend `runtime_display` with an optional `pause_request` projection.

```jsonc
{
  "pause_request": {
    "id": "uuid",
    "kind": "structured_question",
    "status": "pending",
    "provider": "codex",
    "can_respond": true,
    "title": "Choose an approach",
    "summary": "The agent needs your answer before it can continue.",
    "tool_label": null,
    "questions": [
      {
        "id": "storage",
        "header": "Storage",
        "question": "Which storage backend should I implement?",
        "multi_select": false,
        "options": [
          {"label": "SQLite", "description": "Keep it local and simple."},
          {"label": "Postgres", "description": "Use managed database features."}
        ]
      }
    ]
  }
}
```

Display policy:

- `needs_user` plus pending `structured_question`:
  - `needs_attention = true`
  - `tone = "blocked"` in v1, as the existing orange attention tone
  - headline `Needs answer`
  - detail `Question waiting` or provider-specific copy
- `needs_user` without pending pause request:
  - `needs_attention = false`
  - quiet idle/ready copy
- pending pause request with stale/offline control:
  - visible as waiting on the user
  - `can_respond = false`
  - detail tells the user to answer in the terminal or reattach the host
- closed sessions:
  - suppress `pause_request` in `runtime_display`
  - `needs_attention = false`

Compatibility note: v1 deliberately reuses `tone = "blocked"` for structured
question attention because it is the existing orange attention tone. Copy and
`pause_request.kind` carry the semantic distinction. A later cleanup may rename
the tone to `attention`, but that migration is not required to fix the product
bug.

Update `runtime-display-contract.md` after implementation so the invariants say:

```text
needs_attention == true =>
  lifecycle != closed
  and (
    state == blocked
    or pause_request.status == pending
  )

needs_attention == true => tone == blocked

lifecycle == closed => pause_request == null
```

## API Contract

V1 ships the user/browser/iOS route because that is the path that fixes the
visible product bug:

```text
GET  /api/sessions/{session_id}/pause-requests
POST /api/sessions/{session_id}/pause-requests/{pause_request_id}/response
```

Add the machine/agent route before exposing pause responses through MCP or
agent-facing tools:

```text
GET  /api/agents/sessions/{session_id}/pause-requests
POST /api/agents/sessions/{session_id}/pause-requests/{pause_request_id}/response
```

Both route families must use the same service layer.

List response:

```jsonc
{
  "requests": [
    {
      "id": "uuid",
      "session_id": "uuid",
      "kind": "structured_question",
      "status": "pending",
      "provider": "codex",
      "can_respond": true,
      "title": "Choose an approach",
      "questions": [...]
    }
  ],
  "total": 1
}
```

Response request for a structured question:

```jsonc
{
  "decision": "answer",
  "answers": {
    "storage": ["SQLite"]
  },
  "message": "Use SQLite for now."
}
```

Response result:

```jsonc
{
  "status": "resolved",
  "pause_request": {...}
}
```

If the provider cannot be answered remotely:

```http
409 Conflict
```

```jsonc
{
  "detail": {
    "code": "pause_request_not_answerable",
    "message": "Answer this request in the terminal.",
    "pause_request_id": "uuid"
  }
}
```

If the user sends normal chat input while a pending answerable pause request is
active, do not guess that the chat text is an answer. Return a structured
conflict:

```jsonc
{
  "code": "pause_request_pending",
  "message": "Answer the pending provider question before sending a new prompt.",
  "pause_request_id": "uuid"
}
```

Queueing next-turn input while a pause is pending can be added later if product
need is clear, but it must stay visually separate from answering the provider
request.

## Provider Mapping

### Codex

Codex is the first full implementation target because Longhouse already owns
the app-server bridge.

Inputs:

- `item/tool/requestUserInput` -> `structured_question`,
  phase `needs_user`
- `mcpServer/elicitation/request` -> `structured_question`,
  phase `needs_user`
- `thread/status/changed` active flags:
  - `waitingOnUserInput` refreshes an existing structured question only

The explicit JSON-RPC request is authoritative for creating a pause request.
Status flags are level signals; they should refresh `last_seen_at` but not
create independent pause rows.

Codex approval requests such as `item/commandExecution/requestApproval`,
`item/fileChange/requestApproval`, and `item/permissions/requestApproval` stay
on the existing blocked/approval path for this project. Do not add new UX for
them in v1.

Current bug to fix: the Codex bridge currently answers structured user-input
server requests immediately. In user-managed sessions, that is not enough.
However, holding a provider request open is a bridge concurrency change, not a
small handler tweak.

The bridge implementation must:

1. record the pending provider request in `BridgeContext`;
2. emit/persist a pause request to Runtime Host;
3. return to the bridge `select!` loop so IPC remains serviceable;
4. accept a response IPC command from Runtime Host/Machine Agent;
5. send the provider-native JSON-RPC response;
6. emit a pause resolution when provider continues or response delivery fails.

The same deferred-request behavior must work inside nested request/response
loops used while the bridge is waiting for its own app-server request response.

This path must be flag-gated and canary-proven before `can_respond=true` is
shown to users. Existing explicit `auto_approve` canary/debug behavior may keep
immediate responses, but normal managed sessions must not silently auto-answer.

Timeout policy:

- Detection-only mode never holds the provider request for remote response; it
  follows the current provider behavior and marks `can_respond=false`.
- Answerable mode may wait while the bridge is online and a pending request is
  visible.
- If the bridge disconnects, the provider request is failed/expired and the
  pause request is marked accordingly.
- Do not fall back to terminal keystroke automation.

### Claude

Inputs:

- `Notification` with `notification_type=elicitation_dialog` ->
  `structured_question`, phase `needs_user`
- `Notification` with `notification_type=idle_prompt` -> phase-only
  `needs_user`, no pause request

Claude `PermissionRequest` and `permission_prompt` remain existing `blocked`
signals. They are explicitly not the target of this project.

Claude hook payload capture needs verification. If the hook event does not
include full question/options payload, create a minimal non-answerable pause
request:

```text
kind=structured_question
can_respond=false
summary="Question waiting in terminal"
```

Do not claim Claude answerability until a provider-native response path is
proven. Hooks are enough for detection and notifications; they are not by
themselves a response channel.

### OpenCode

Inputs:

- documented question events, once confirmed by provider-live evidence ->
  `structured_question`, phase `needs_user`

OpenCode managed sessions are currently observe-only at the active-turn steer
layer. Initial implementation should detect and display pause requests. Remote
answering must wait for a proven server-bridge response surface.

OpenCode permission events remain on the existing blocked path.

### Antigravity

Inputs:

- `ask_question`, once confirmed -> `structured_question`

Current Longhouse hook only emits `thinking`, `running`, and `idle`. The first
implementation should make this gap visible in tests. Provider-specific event
capture should land after the exact Antigravity payload shape is proven.

Initial Antigravity pause requests should be non-answerable unless the
hook-inbox adapter proves a stable answer path.

## Notification Policy

Add a separate event class for structured questions:

```text
session_needs_answer
```

Policy:

- `structured_question`: in-app attention and Live Activity/widget updates
  always; APNs alert only when notification presence policy says the user is
  not already watching Longhouse.
- `blocked` permission approvals: preserve the current path; do not expand or
  redesign it here.
- `idle_prompt`: no immediate alert; belongs to the long-run-waiting policy.

This preserves the previous distinction from `session-alerting-research-spike`:
ordinary `needs_user` stays quiet, but actionable structured questions do not.

Resolution cleanup must cover `session_needs_answer`, including the case where
the user answered in the terminal and Longhouse only observes provider
continuation afterward.

Notification collapse/dedupe keys should derive from the pause request
`request_key`, not only from session id, so a superseded question does not leave
stale delivered notification state behind.

## UI Requirements

Timeline card:

- show `Needs answer` for pending structured questions;
- keep orange/attention styling for structured questions;
- do not show ordinary `needs_user` as attention.
- do not change the existing permission approval card behavior.

Session detail:

- show a pause panel above or near the composer while active;
- for answerable structured questions, render provider-native choices;
- for non-answerable requests, explain where to answer;
- do not allow a generic chat send to masquerade as answering a provider-native
  question.

iOS:

- mirror the server `runtime_display.pause_request`;
- include pending pause requests in attention ordering;
- show `Needs answer` in session detail and widgets;
- support response UI only for answerable providers.

## QA Strategy

Do not rely on a real terminal TUI for base coverage.

Fixture-first tests:

- Codex fake app-server emits `requestUserInput` JSON-RPC requests.
- Claude hook fixture posts `elicitation_dialog` and `idle_prompt`.
- OpenCode plugin fixture emits confirmed question events.
- Antigravity hook fixture emits confirmed question event shapes.

Provider-live tests:

- Extend Codex app-server canary to assert pending pause request creation,
  response dispatch, and resolution.
- Extend OpenCode/Antigravity provider-live canaries only after the event/answer
  surface is confirmed locally.
- Claude live canary should at minimum prove hook-derived pause display; remote
  answering is a separate contract.

Client tests:

- web timeline/session detail render pending structured question as attention;
- iOS model tests decode `pause_request` and include it in attention ordering;
- notification tests distinguish `session_needs_answer` from quiet
  `needs_user`.

## Implementation Plan

### Phase 1 - Server Kernel And Display

- Add `SessionPauseRequest` model with the trimmed schema above.
- Add pause-request service helpers:
  - upsert request;
  - resolve/supersede/expire;
  - load active request for sessions;
  - serialize public projection.
- Add pause event handling that writes pause rows only, not phase.
- Extend `session_views` and `runtime_display` with optional
  `pause_request`.
- Reconcile display invariants:
  - pending pause request means `needs_attention=true`;
  - pending pause request means `tone="blocked"` in v1;
  - closed sessions suppress pause request display.
- Add synthetic backend tests and runtime-display snapshots.

Deliverable: backend tests can create `needs_user + structured_question` and get
`needs_attention=true` without changing ordinary `needs_user`.

### Phase 2 - Codex Held-Request Spike

- Behind an explicit debug/feature flag, prove the bridge can defer a provider
  server request without deadlocking.
- Store pending provider request data in bridge context.
- Return to the bridge event loop while a request is pending.
- Add an IPC command for responding to the pending provider request.
- Handle server requests that arrive inside nested app-server request/response
  loops.
- Add timeout/disconnect failure handling.
- Prove the behavior in the fake app-server canary.

Deliverable: technical proof that Codex remote answering is feasible and does
not block the bridge from receiving the answer.

### Phase 3 - Codex Detection-Only End To End

- Emit `pause_request` / `pause_resolution` from the Codex bridge.
- Keep `can_respond=false` initially.
- Preserve existing auto-answer/decline behavior outside the held-request flag.
- Prove timeline/session detail can show `Needs answer` from Codex provider
  events without implementing remote response UI.

Deliverable: the visible "frozen session" bug is fixed for Codex even before
remote answer support.

### Phase 4 - Codex Answering

- Wire the response route to a machine command and bridge IPC response.
- Flip `can_respond=true` for Codex only when the bridge held-request path is
  enabled and proven live.
- Add tests for:
  - answer structured question;
  - provider disconnect while pending;
  - normal chat send returns `pause_request_pending`.

Deliverable: answer a Codex multiple-choice question from Longhouse without
touching the terminal.

### Phase 5 - Web And iOS Response UI

- Regenerate OpenAPI clients.
- Web: timeline/session detail pause panel, answer form, and response errors.
- iOS: decode/render pause request, attention ordering, answer UI for
  answerable requests.
- Widgets/Live Activity: show `Needs answer` from server projection.

Deliverable: iOS no longer makes a structured question pause look frozen or
quiet, and answerable Codex requests can be handled from the app.

### Phase 6 - Claude/OpenCode/Antigravity Detection

- Claude: create pause requests from verified hook payloads; mark
  non-answerable unless a native response path exists.
- OpenCode: add structured-question pause request mapping from provider events.
- Antigravity: add structured-question pause request mapping after payload
  shape is proven.
- Add provider-live evidence for each provider surface that claims support.

Deliverable: every supported provider either shows a real pause request or has
an explicit unsupported/unknown gap backed by tests.

### Phase 7 - Notifications

- Add `session_needs_answer` notification event type.
- Add APNs copy for structured questions.
- Apply presence gating for structured-question APNs alerts.
- Ensure resolution cleanup handles both event types.
- Preserve long-run-waiting as a separate policy.

Deliverable: phone alerts distinguish answer needed from long run ready, while
existing permission alerts remain unchanged.

## Success Criteria

- A Codex `requestUserInput` request renders as `Needs answer`, not quiet
  `Idle`.
- Ordinary `needs_user` without a pause request stays quiet.
- A pending pause request remains visible until explicitly resolved, superseded,
  failed, expired, or session-closed; it does not vanish after the 10-minute
  `needs_user` phase freshness window.
- Detection-only provider requests clearly say to answer in the terminal when
  remote response is unavailable.
- Answerable provider requests can be answered from Longhouse through a
  provider-native response path.
- Web and iOS render from the same server projection.
- APNs distinguishes structured-question alerts from quiet `needs_user`.
- Fixture tests cover provider event shapes without requiring a TUI.
- Provider-live canaries prove every support claim that depends on a real
  upstream provider.

## Open Questions

- Does Claude Code's hook payload expose full `AskUserQuestion` questions and
  options, or only an `elicitation_dialog` notification?
- What is the exact OpenCode event payload for structured questions in the
  current supported release?
- What is the exact Antigravity hook payload for `ask_question` in the current
  supported release?
- Should `tone="blocked"` remain the compatibility orange attention tone, or
  should we migrate to a clearer `tone="attention"` value after the first
  implementation?
- Should explicit queue-next-input while a pause request is pending be allowed
  later, or should all new input wait until the provider pause resolves?
