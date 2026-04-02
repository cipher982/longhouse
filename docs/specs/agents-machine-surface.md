# Agents Machine Surface

Status: Active canon
Last updated: 2026-04-02

## Goal

Declare the machine-facing contract for Longhouse's session kernel and coordination primitives.

This is the surface agents, CLIs, scripts, CI jobs, and background automations should target first. MCP and browser routes can wrap or mirror these capabilities, but they are not the foundation.

## Rules

- `/api/agents/*` is the canonical machine namespace for session archive, coordination, search/recall, briefing, reflection, and message flows.
- CLI commands should sit directly on top of these routes.
- MCP tools should sit on top of the same routes or the same service layer.
- Browser routes may reuse the same response models or service helpers, but machine clients should not depend on browser-owned endpoints.
- If a new capability matters to agents, it should land here before or alongside any MCP or browser integration.

## Authentication

### Canonical machine auth

- Machine clients authenticate with `X-Agents-Token`.
- The normal machine token is a device token (`zdt_*`).
- The agents surface is single-tenant only for now. Multi-tenant behavior is intentionally not part of this contract yet.

### Managed-local hook token exception

Managed-local hook tokens are intentionally narrow and are only valid for:

- `GET /api/agents/sessions`
- `POST /api/agents/ingest`
- `POST /api/agents/presence`

They exist to support managed-local shipping and presence hooks, not to grant broad machine API access.

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

- `LONGHOUSE_SESSION_ID` is the process-level source of current session identity.
- The CLI and MCP layers translate that into `X-Longhouse-Session-Id` when they call the API.

## Response Conventions

- Responses are JSON-only.
- UUIDs are serialized as strings.
- Timestamps are ISO-8601 UTC strings.
- List responses use stable envelopes like `{sessions, total}`, `{events, total}`, `{messages, total}`, or `{insights, total}`.
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

### Coordination and directed messaging

- `POST /api/agents/messages`
- `GET /api/agents/messages`
- `POST /api/agents/messages/{message_id}/ack`

Current delivery model:

- durable message row first
- safe-boundary delivery attempt for managed-local sessions
- drain up to 10 queued messages while the target remains in a deliverable state
- explicit acknowledgement from the target session
- non-live sessions can still poll the durable inbox

### Continuity and project context

- `GET /api/agents/recall`
- `GET /api/agents/briefing`
- `POST /api/agents/reflect`
- `GET /api/agents/reflections`
- `GET /api/agents/insights`
- `POST /api/agents/insights`

Compatibility note:

- `POST /api/insights` remains supported for existing machine callers, but new machine clients should use `POST /api/agents/insights`.

## Browser Relationship

The browser owns presentation-first routes such as `/api/timeline/*` and browser-auth insight reads/archive actions under `/api/insights`.

That browser surface is a veneer, not the canon:

- browser routes use browser auth and browser-specific UX concerns
- machine clients should prefer `/api/agents/*`
- new machine-first features should not launch only under `/api/timeline/*`
- duplicated browser and machine routes should share service logic where practical, but contract ownership stays with the machine surface

Examples:

- `/api/timeline/sessions` is a browser archive feed
- `/api/agents/sessions` is the canonical machine session listing/search surface
- `/api/insights` `GET` is browser-owned
- `/api/agents/insights` `GET/POST` is machine-owned

## CLI Parity

The current CLI contract sits directly on the canonical machine surface:

- `longhouse peers`
- `longhouse message`
- `longhouse tail`
- `longhouse check-messages`
- `longhouse ack-message`
- `longhouse sessions get`
- `longhouse sessions events`

The rule going forward is simple: if a coordination or session-inspection primitive matters, it should be reachable by raw HTTP and `longhouse ...` before treating MCP as complete.

## Non-Goals

- This does not promise cross-org federation, AGNTCY-style discovery, or A2A compatibility yet.
- This does not collapse every browser route into `/api/agents/*`.
- This does not make Oikos the machine boundary. Oikos should consume this surface like any other agent-capable client.
