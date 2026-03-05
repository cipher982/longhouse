# Oikos Multi-Surface Messaging (Web + Telegram + Future Surfaces)

Date: 2026-03-04
Status: Implemented (2026-03-05)

## Why This Exists

Telegram is now live as an Oikos surface. The current implementation routes Telegram and web turns into one visible mixed timeline, which creates UI confusion even though the shared-thread "one brain" reasoning model is correct.

This spec defines the final architecture for:
- one canonical Oikos reasoning thread per user
- surface-filtered user views (web, Telegram, voice, future channels)
- reliable transport semantics (dedupe + run serialization)
- deterministic reprovision behavior for per-instance channel/provider env vars

## Product Alignment (from VISION + Landing)

- Timeline/session interaction remains the primary product surface.
- Oikos is a secondary coordinator layer.
- Chat channels (Telegram, future channels) are tertiary "away interfaces".
- Therefore, channel integration must not degrade the web UX.

## Architecture Decision

Keep one canonical Oikos thread and store per-message surface metadata.
Render surface-specific projections in history APIs/UI.

Rejected:
- one thread per surface (fragments memory/context)
- mixed unfiltered thread view as default (breaks UX)

## Principles

1. One canonical reasoning memory; many filtered presentation views.
2. Surface adapters should be thin transport bridges.
3. Metadata should separate origin from delivery.
4. No runtime DB migration required for surface tagging (`ThreadMessage.message_metadata` already exists).
5. Web default must remain clean and predictable.

## Data Contract (message_metadata)

Use `ThreadMessage.message_metadata.surface` as canonical envelope:

```json
{
  "surface": {
    "origin_surface_id": "web|telegram|voice|system",
    "origin_conversation_id": "web:main|telegram:6311583060|voice:default|...",
    "source_message_id": "platform message id if inbound",
    "source_event_id": "transport event id (ex: telegram update_id)",

    "delivery_surface_id": "web|telegram|voice|null",
    "delivery_conversation_id": "target conversation id if delivered",

    "visibility": "surface-local|cross-surface|internal",

    "idempotency_key": "channel-scoped dedupe key"
  }
}
```

Notes:
- `origin_*` answers where intent entered the system.
- `delivery_*` answers where this rendered response was delivered.
- `visibility` controls projection behavior.
- For old rows without metadata: treat as `origin_surface_id=web`, `delivery_surface_id=web`, `visibility=surface-local`.

## Surface Semantics

For normal request/response on one surface:
- user row: origin=surface, delivery=surface, visibility=surface-local
- assistant row: origin=surface, delivery=surface, visibility=surface-local

For proactive tool-driven sends (future-safe contract):
- optional assistant row can be persisted with origin=`system` (or triggering surface), delivery=`telegram`, visibility=`surface-local`.

## Execution Contract Changes

### 1) OikosService entrypoint

Extend `run_oikos()` with source context:

```python
async def run_oikos(
    ...,
    source_surface_id: str = "web",
    source_conversation_id: str = "web:main",
    source_message_id: str | None = None,
    source_event_id: str | None = None,
)
```

### 2) Callers pass source context

- `routers/oikos_chat.py`:
  - `source_surface_id="web"`
  - `source_conversation_id="web:main"`
  - `source_message_id=request.message_id`
- `services/telegram_bridge.py`:
  - `source_surface_id="telegram"`
  - `source_conversation_id=f"telegram:{chat_id}"`
  - `source_message_id=event.message_id`
  - `source_event_id=event.raw.update_id` (requires plugin to include it)
- `voice/turn_based.py`:
  - `source_surface_id="voice"`
  - `source_conversation_id="voice:default"`

### 3) Persist metadata on both user + assistant messages

`crud.create_thread_message()` must accept `message_metadata` and write it through to model.

## History API Contract

### Endpoint

`GET /api/oikos/history`

### Query params

- `surface_id` (default `web`)
- `view` (`surface` default, `all` optional)

### Projection rules

- `view=surface`:
  - include rows with `visibility != internal`
  - include if `origin_surface_id == surface_id` OR `delivery_surface_id == surface_id`
- `view=all`:
  - include all non-internal rows regardless of surface

### Response additions

Extend `OikosChatMessage` with optional metadata fields for UI badges/toggles:
- `origin_surface_id`
- `delivery_surface_id`
- `visibility`

Existing fields remain unchanged for backward compatibility.

## Dedupe Contract (Telegram retries)

Inbound Telegram messages must dedupe before starting Oikos:

- key: `telegram:{chat_id}:{update_id}` (required)
- write key to `message_metadata.surface.idempotency_key`
- pre-run check: if message with same key already exists in Oikos thread, skip execution and ACK webhook
- fail-closed behavior:
  - missing `update_id` => skip run
  - dedupe lookup errors => skip run

Implementation phase:
- Phase 1: application-level lookup in thread messages (no new table)
- Phase 2 (if needed at scale): dedicated dedupe table with unique constraint

## Per-User Run Serialization

Need one active Oikos run per owner across all surfaces to avoid racey interleaving on shared thread.

Phase 1:
- process-local `asyncio.Lock` keyed by `owner_id` around `run_oikos()` invocation path

Phase 2 (if multi-process needed):
- DB/redis-backed distributed lock or queued dispatcher

## Control Plane Reprovision Contract (Env Durability)

Current risk: reprovision rebuilds env from static control-plane defaults and drops per-instance vars (`TELEGRAM_*`, instance-specific OpenAI values).

Target behavior:
- per-instance custom env is part of instance desired state
- provision + reprovision deterministically merge:
  - base env from control-plane defaults
  - custom per-instance env overlay

Proposed minimal implementation:
- add `cp_instances.custom_env_json` (text/json)
- extend `_env_for(...)` to merge custom env with denylist for core-owned keys
- make reprovision/regenerate-password/deployer paths use the same merged env builder

## File-Level Change Plan

Runtime app:
- `apps/zerg/backend/zerg/services/oikos_service.py`
- `apps/zerg/backend/zerg/crud/crud_messages.py`
- `apps/zerg/backend/zerg/schemas/schemas.py`
- `apps/zerg/backend/zerg/routers/oikos.py`
- `apps/zerg/backend/zerg/routers/oikos_chat.py`
- `apps/zerg/backend/zerg/services/telegram_bridge.py`
- `apps/zerg/backend/zerg/channels/plugins/telegram.py` (expose `update_id` in raw event)
- `apps/zerg/frontend-web/src/oikos/lib/oikos-chat-controller.ts` (`surface_id=web`)
- `apps/zerg/frontend-web/src/oikos/app/...` (optional all-activity toggle + badges)

Control plane:
- `apps/control-plane/control_plane/models.py`
- `apps/control-plane/control_plane/main.py` (lightweight column migration pattern)
- `apps/control-plane/control_plane/services/provisioner.py`
- `apps/control-plane/control_plane/routers/instances.py`
- control-plane tests under `apps/control-plane/tests/`

## Rollout Phases

### Phase A: Surface Metadata + Filtered History (shipped)

- pass source context through callers
- persist metadata on user+assistant rows
- add `/api/oikos/history` surface filter (`surface_id=web` default)
- frontend includes `surface_id=web` in history load

Outcome:
- web UI stops showing Telegram turns by default
- shared reasoning context remains intact

### Phase B: Reliability Hardening (shipped)

- Telegram inbound dedupe
- per-user run serialization

Outcome:
- retries/races stop creating duplicate or interleaved turn artifacts

### Phase C: UX + Expansion (shipped)

- all-activity toggle in web UI
- surface badges
- proactive cross-surface event rendering policy
- add new surface adapters (WhatsApp/SMS/etc.) by contract

## Acceptance Criteria

1. Web Oikos history default contains only web-surface turns.
2. Telegram conversation shows Telegram turns, not web-only chatter.
3. Oikos responses still use full shared thread context for reasoning.
4. Telegram webhook retries do not duplicate user rows or runs.
5. Concurrent web+Telegram prompts for same user execute serially.
6. Reprovision no longer drops per-instance Telegram/OpenAI settings.

Status: All six criteria implemented in code and verified in test/live checks on 2026-03-05.

## Non-Goals (v1)

- no split into separate per-surface threads
- no new dedicated runtime table for surface metadata
- no timer-driven proactive notification automation heuristics

## Remaining Questions

1. Should proactive `send_telegram` persist explicit assistant timeline rows, or remain tool-result-only?
2. Should per-owner run serialization move from process-local lock to distributed lock for multi-process scale?
3. When adding new channels, should we add a channel-agnostic dedupe ledger table now or keep metadata-only dedupe until load requires it?

## Implementation Notes

- Existing `message_metadata` column removes need for runtime schema migration for surface tagging.
- `ThreadMessageResponse` currently hides metadata; expose only the surface subset needed by UI.
- Keep agent-context loading unfiltered; only projection APIs/UI should filter by surface.
