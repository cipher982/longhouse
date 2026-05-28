# Agents Machine Surface

Status: Active canon
Last updated: 2026-04-18

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

### Import / presence hook token exception

Shipper and presence hook tokens are intentionally narrow and are only valid for:

- `GET /api/agents/sessions`
- `POST /api/agents/ingest`
- `POST /api/agents/presence`

They exist to support session import and presence reporting, not to grant broad machine API access.

## Session Context

Some machine actions act "as" a specific session instead of just "as" a device.

### Canonical request header

- `X-Longhouse-Session-Id: <session-uuid>`

Use it for directed session actions such as:

- `POST /api/agents/messages`
- `GET /api/agents/messages`
- `POST /api/agents/messages/{id}/ack`

### Resolution rules

- If the authenticated token already carries session identity, the server treats that as the source of truth.
- If both token session context and `X-Longhouse-Session-Id` are present, they must match.
- If the request body also declares a session id, it must match the authenticated/current session context.
- Requests that need session context and provide none should fail fast.

### CLI and MCP source of session context

- Longhouse-managed launchers inject current session context into the process environment for the running session.
- The CLI and MCP layers translate current managed-session context into `X-Longhouse-Session-Id` when they call the API.

## Response Conventions

- Responses are JSON-only.
- UUIDs are serialized as strings.
- Timestamps are ISO-8601 UTC strings.
- List responses use stable envelopes like `{sessions, total}`, `{events, total}`, `{messages, total}`, or `{turns, total}`.
- Directed message payloads use explicit delivery fields instead of inferring state from fetch behavior.
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

### Machine health and transport summaries

- `GET /api/agents/machines/health`
- `POST /api/agents/machines/{device_id}/provider-live-proof`

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
live proof per owner/device/provider to avoid duplicate token-spending canaries.
`make provider-live-route-e2e` verifies this hosted route end to end against a
configured dogfood machine: machine directory support, positive version match,
and typed `provider_version_mismatch` rejection. `make dogfood-refresh` writes
the latest hosted-route proof to
`~/.longhouse/provider-live-route-e2e/latest.json`; local-health and doctor read
that sidecar as the durable evidence that hosted dispatch still works for this
machine. The default provider set is `auto`: every current valid
`~/.longhouse/provider-live-proof/{provider}.json` sidecar is routed through the
hosted machine API. Local-health reports coverage separately so a one-provider
green route proof cannot masquerade as all-provider coverage.

### Coordination and directed messaging

- `POST /api/agents/messages`
- `GET /api/agents/messages`
- `POST /api/agents/messages/{message_id}/ack`
Current delivery model:

- durable message row first
- safe-boundary delivery attempt when the target session has a live control path
- drain up to 10 queued messages while the target remains in a deliverable state
- explicit acknowledgement from the target session
- non-live sessions can still poll the durable inbox
- wall entries now surface `pending_inbound_messages` so agents can see which sessions already have unacknowledged inbound work

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
- `/api/timeline/sessions/{session_id}/turns` is a browser inspection route
- `/api/agents/sessions/{session_id}/turns` is the canonical machine-facing turn surface

## CLI Parity

The current CLI contract sits directly on the canonical machine surface:

- `longhouse wall`
- `longhouse peers`
- `longhouse message`
- `longhouse continue`
- `longhouse tail`
- `longhouse messages`
- `longhouse messages ack`
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

### Send a directed session message

```bash
curl -s \
  -H "X-Agents-Token: $LONGHOUSE_TOKEN" \
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \
  -H "Content-Type: application/json" \
  -d '{"to_session_id":"'"$TARGET_SESSION_ID"'","text":"Please inspect the failing test and report back."}' \
  "$LONGHOUSE_URL/api/agents/messages"
```

```bash
longhouse message "$TARGET_SESSION_ID" "Please inspect the failing test and report back." --json
```

### Read and acknowledge the durable inbox

```bash
curl -s \
  -H "X-Agents-Token: $LONGHOUSE_TOKEN" \
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \
  "$LONGHOUSE_URL/api/agents/messages?direction=inbound&unacknowledged_only=true&limit=20"
```

```bash
longhouse messages --json
```

```bash
curl -s \
  -X POST \
  -H "X-Agents-Token: $LONGHOUSE_TOKEN" \
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \
  -H "Content-Type: application/json" \
  -d '{}' \
  "$LONGHOUSE_URL/api/agents/messages/$MESSAGE_ID/ack"
```

```bash
longhouse messages ack "$MESSAGE_ID" --json
```

## Non-Goals

- This does not promise cross-org federation, AGNTCY-style discovery, or A2A compatibility yet.
- This does not collapse every browser route into `/api/agents/*`.
- This does not make any assistant surface the machine boundary. Browser, MCP, and native clients should consume this surface like any other agent-capable client.
