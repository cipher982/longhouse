# Agents Machine Surface

Status: Active canon
Last updated: 2026-07-23

## Goal

Declare the machine-facing contract for Longhouse's session kernel and coordination primitives.

This is the surface agents, CLIs, scripts, CI jobs, and background automations should target first. MCP and browser routes can wrap or mirror these capabilities, but they are not the foundation.

## Rules

- `/api/agents/*` is the canonical machine namespace for session archive, coordination, search/recall, and message flows.
- CLI commands should sit directly on top of these routes.
- MCP tools should sit on top of the same routes or the same service layer.
- Browser routes may reuse the same response models or service helpers, but machine clients should not depend on browser-owned endpoints.
- If a new capability matters to agents, it should land here before or alongside any MCP or browser integration.

## Authentication

### Canonical machine auth

- Machine clients authenticate with `X-Agents-Token`.
- The normal machine token is a device token (`zdt_*`).
- The agents surface is single-tenant only for now. Multi-tenant behavior is intentionally not part of this contract yet.

### Managed-session token scopes

Managed launches receive signed session credentials (`zst_*`) with one narrow
scope. Hook-scoped credentials are valid only for:

- `GET /api/agents/sessions`
- `GET /api/agents/sessions/stream` — SSE cold snapshot followed by
  commit-driven session upserts/removals; machine clients reconnect and replay
  instead of polling
- `POST /api/agents/ingest`
- `POST /api/agents/presence`

Coordination-scoped credentials are valid only for:

- `POST /api/agents/directed-inputs`
- `GET /api/agents/directed-inputs`
- `POST /api/agents/directed-inputs/{id}/reply`

The signed credential binds owner, device, and session. The session UUID header
is context that must match the credential; it is never authority by itself.

## Session Context

Some machine actions act "as" a specific session instead of just "as" a device.

### Canonical request header

- `X-Longhouse-Session-Id: <session-uuid>`

Use it for directed session actions such as:

- `POST /api/agents/directed-inputs`
- `GET /api/agents/directed-inputs`
- `POST /api/agents/directed-inputs/{id}/reply`

### Resolution rules

- If the authenticated token already carries session identity, the server treats that as the source of truth.
- If both token session context and `X-Longhouse-Session-Id` are present, they must match.
- If the request body also declares a session id, it must match the authenticated/current session context.
- Requests that need session context and provide none should fail fast.

### CLI and MCP source of session context

- Longhouse-managed launchers give the registered coordination adapter a
  launch-scoped credential and current-session context.
- Nested provider processes do not inherit coordination authority.
- CLI and MCP translate the adapter context into the session header and signed
  credential when they call the API.

## Response Conventions

- Responses are JSON-only.
- UUIDs are serialized as strings.
- Timestamps are ISO-8601 UTC strings.
- List responses use stable envelopes like `{sessions, total}`, `{events, total}`, `{directed_inputs, next_cursor}`, or `{turns, total}`.
- Directed-input payloads expose their linked provider input receipt facts.
  An absent receipt means no live delivery attempt was made.
- Machine errors should use normal HTTP status codes plus JSON `detail`.

## Canonical Route Families

### Session archive and inspection

- `GET /api/agents/sessions`
- `GET /api/agents/sessions/summary`
- `GET /api/agents/sessions/wall`
- `GET /api/agents/sessions/active`
- `GET /api/agents/sessions/semantic`
- `GET /api/agents/sessions/{session_id}`
- `GET /api/agents/sessions/{session_id}/events`
- `GET /api/agents/sessions/{session_id}/tail`
- `GET /api/agents/sessions/{session_id}/thread`
- `GET /api/agents/sessions/{session_id}/projection`
- `GET /api/agents/sessions/{session_id}/workspace`
- `GET /api/agents/sessions/{session_id}/preview`
- `GET /api/agents/sessions/{session_id}/export`
- `GET /api/agents/sessions/{session_id}/turns`
- `GET /api/agents/sessions/{session_id}/turns/{turn_id}`
- `GET /api/agents/worklog/day` — one timestamp-anchored day export for
  machine worklog consumers; see `worklog-day-export-api.md`

### Machine health and transport summaries

- `GET /api/agents/machines` — enrolled machine directory plus current control
  operations and the canonical human-launch projection
- `GET /api/agents/machines/health`
- `POST /api/agents/machines/{device_id}/provider-live-proof`

The directory preserves raw `supports` and provider operations for agents and
diagnostics. Human clients consume its derived `launch` projection instead of
reconstructing launchability or defaults from those raw fields. The
user-authenticated `/api/timeline/machines` route is a veneer over the same
service projection and response model.

This route is the canonical machine-facing summary for latest heartbeat-derived
transport state. It should answer, per device, whether Longhouse shipping is
healthy, degraded, offline, or broken, plus the dominant recent transport
symptom and rolling ship outcome counters.

Provider live proof is a typed Machine Agent command, not generic remote shell.
The Runtime Host dispatches `provider.live_proof` only to a connected machine
that advertises `{provider}.live_proof`, and the Machine Agent runs the local
packaged `longhouse provider-live` canary with the user's installed provider CLI.
This lets release automation ask a provider-capable user-owned machine for live
evidence without installing Claude, Codex, OpenCode, or Antigravity inside the
automation container. Callers may include `expected_provider_version`; when the
returned live artifact proves a different version, the Machine Agent rejects the
command with `provider_version_mismatch`; Runtime Host maps that mismatch to an
application-level conflict so release automation cannot promote a local green
proof for the wrong upstream release and edge proxies do not collapse the typed
body into a generic upstream 5xx. Runtime Host also allows only one in-flight
live proof per owner/device/provider to avoid duplicate provider-response canaries.
`make provider-live-route-e2e` verifies this hosted route end to end against a
configured dogfood machine: machine directory support, positive version match,
and typed `provider_version_mismatch` rejection. `make dogfood-refresh` writes
the latest hosted-route proof to
`~/.longhouse/provider-live-route-e2e/latest.json`; local-health and doctor read
that sidecar as the durable evidence that hosted dispatch still works for this
machine. The default provider set is `auto`: every current valid shared
live-proof sidecar for Claude, OpenCode, or Antigravity is routed through the
hosted machine API. Local-health reports coverage separately so a one-provider
green route proof cannot masquerade as all-provider coverage. The route harness
retries transient hosted dispatch failures per provider; typed version
mismatches and provider verdict failures remain strict evidence.

### Coordination and directed input

- `POST /api/agents/directed-inputs`
- `GET /api/agents/directed-inputs`
- `POST /api/agents/directed-inputs/{directed_input_id}/reply`
- `POST /api/agents/sessions/{session_id}/coordination-token` — device-authorized
  issuance for a managed adapter on that exact owner/device/session

Current delivery contract:

- persist the directed-input envelope first;
- use the existing managed session-input receipt as the only delivery path;
- inject at a proved quiescent boundary or queue behind an active turn;
- do not steer an active turn and do not start or resume a cold session;
- keep observe-only or unavailable targets durable without claiming an attempt;
- recover all inbound and outbound inputs by stable id cursor; and
- correlate replies with `reply_to_id` instead of acknowledgement state.

### Project context

- `GET /api/agents/recall`
- `GET /api/agents/sessions/startup-context` — consumed by the opt-in
  [startup-continuity lab](../../labs/startup-continuity/README.md); not part
  of the launch promise

## Browser Relationship

The browser owns presentation-first routes such as `/api/timeline/*`.

That browser surface is a veneer, not the canon:

- browser routes use browser auth and browser-specific UX concerns
- hosted iOS companion flows are user-auth clients too, so they stay on the
  browser/user surface instead of presenting a machine token
- machine clients should prefer `/api/agents/*`
- new machine-first features should not launch only under `/api/timeline/*`
- duplicated browser and machine routes should share service logic where practical, but contract ownership stays with the machine surface

Examples:

- `/api/timeline/sessions` is a browser archive feed
- `/api/agents/sessions` is the canonical machine session listing/search surface
- `/api/agents/sessions/stream` is the canonical machine realtime projection;
  `/api/timeline/sessions/stream` is its browser/user-auth veneer
- `/api/timeline/sessions/{session_id}/turns` is a browser inspection route
- `/api/agents/sessions/{session_id}/turns` is the canonical machine-facing turn surface

## CLI Parity

The current CLI contract sits directly on the canonical machine surface:

- `longhouse wall`
- `longhouse peers`
- `longhouse tail`
- `longhouse send`
- `longhouse inbox`
- `longhouse reply`
- `longhouse sessions get`
- `longhouse sessions events`

The rule going forward is simple: if a coordination or session-inspection primitive matters, it should be reachable by raw HTTP and `longhouse ...` before treating MCP as complete.

## Common Coordination Flows

These are the shortest useful machine flows for external agents and scripts.
When a CLI example omits `--from-session` or `--session`, it assumes the command is running inside a Longhouse-managed session.

### Read the raw wall

```bash
curl -s \
  -H "X-Agents-Token: $LONGHOUSE_TOKEN" \
  "$LONGHOUSE_URL/api/agents/sessions/wall?repo=longhouse&days=7&limit=20"
```

```bash
longhouse wall --repo longhouse --json
```

### Send directed input

```bash
curl -s \
  -H "X-Agents-Token: $LONGHOUSE_COORDINATION_TOKEN" \
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \
  -H "Content-Type: application/json" \
  -d '{"target_session_id":"'"$TARGET_SESSION_ID"'","text":"Please inspect the failing test and report back.","client_request_id":"review-test-1"}' \
  "$LONGHOUSE_URL/api/agents/directed-inputs"
```

```bash
longhouse send "$TARGET_SESSION_ID" "Please inspect the failing test and report back." --client-request-id review-test-1 --json
```

### Recover the durable inbox and reply

```bash
curl -s \
  -H "X-Agents-Token: $LONGHOUSE_COORDINATION_TOKEN" \
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \
  "$LONGHOUSE_URL/api/agents/directed-inputs?direction=inbound&after_id=0&limit=20"
```

```bash
longhouse inbox --json
```

```bash
curl -s \
  -X POST \
  -H "X-Agents-Token: $LONGHOUSE_COORDINATION_TOKEN" \
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \
  -H "Content-Type: application/json" \
  -d '{}' \
  -d '{"text":"The test is fixed.","client_request_id":"review-test-reply-1"}' \
  "$LONGHOUSE_URL/api/agents/directed-inputs/$INPUT_ID/reply"
```

```bash
longhouse reply "$INPUT_ID" "The test is fixed." --client-request-id review-test-reply-1 --json
```

## Non-Goals

- This does not promise cross-org federation, AGNTCY-style discovery, or A2A compatibility yet.
- This does not collapse every browser route into `/api/agents/*`.
- This does not make any assistant surface the machine boundary. Browser, MCP, and native clients should consume this surface like any other agent-capable client.
