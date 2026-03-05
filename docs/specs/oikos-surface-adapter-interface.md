# Oikos Surface Adapter Interface (Web, Telegram, Voice, Future)

Date: 2026-03-05
Status: Draft v1 (research + implementation spec)

## Summary

Define a first-class `SurfaceAdapter` layer so every Oikos surface (web, telegram, voice, future channels) follows one contract for ingress, identity resolution, idempotency, orchestration, and delivery.

This keeps one canonical Oikos reasoning thread while making transport integrations modular.

## Why This Exists

Multi-surface behavior is now shipped, but integration logic still lives in multiple entrypoints:
- web: `routers/oikos_chat.py`
- telegram: `services/telegram_bridge.py`
- voice: `voice/turn_based.py`

The result works, but new surfaces still require touching core Oikos flow in multiple places.

## First-Principles Constraints

1. Oikos reasoning state must remain canonical and surface-agnostic.
2. External transports are not exactly-once. The system must be idempotent by contract.
3. Ingress validation must be fail-closed (no silent fallback keys or implicit defaults).
4. Transport mechanics and Oikos orchestration must be separate concerns.
5. A new surface should require only adapter registration, not edits across Oikos core.

## Research Anchors (Why these constraints are non-negotiable)

- Telegram updates have an `update_id` used to identify updates and can be used for strict dedupe keys.
  - https://core.telegram.org/bots/api#update
- Webhook/event systems generally deliver at-least-once and can be retried/out-of-order.
  - https://docs.stripe.com/webhooks
- Matrix client APIs use explicit transaction IDs (`txnId`) for idempotent send semantics.
  - https://spec.matrix.org/legacy/client_server/r0.6.0#put-matrix-client-r0-rooms-roomid-send-eventtype-txnid
- CloudEvents defines a minimal event envelope (`id`, `source`, `specversion`, `type`) reinforcing stable cross-system event contracts.
  - https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md

Inference from sources: strict event identity + idempotent ingress is the durable design for multi-surface messaging.

## Current Reality (Code)

- Transport plugin architecture already exists (`zerg/channels/*`) and should be retained.
- Telegram transport is wired through `TelegramChannel` + webhook router.
- Telegram-to-Oikos bridging is custom (`TelegramBridge`) and not shared with web/voice.
- Surface metadata and filtered history are already in place (`message_metadata.surface`, `/api/oikos/history`).

## Target Architecture

```text
[Transport Layer]
  Web API / Telegram Webhook / Voice Input
        |
        v
[SurfaceAdapter]
  normalize + validate + owner resolution + dedupe key
        |
        v
[SurfaceOrchestrator]
  claim idempotency + serialize owner run + invoke run_oikos
        |
        v
[OikosService]
  canonical thread + metadata persistence
        |
        +--> [Delivery Dispatcher]
                -> same surface (inline/push)
                -> optional cross-surface delivery
```

## Canonical Contracts

### 1) Normalized ingress event

```python
@dataclass(frozen=True)
class SurfaceInboundEvent:
    surface_id: str                     # web | telegram | voice | ...
    conversation_id: str                # web:main, telegram:<chat_id>, voice:default, ...
    dedupe_key: str                     # REQUIRED, adapter-specific but stable
    owner_hint: str | None              # optional transport hint (chat_id, user_id, device_id)
    source_message_id: str | None       # platform message ID if present
    source_event_id: str | None         # platform event/update ID if present
    text: str                           # normalized user text payload
    timestamp_utc: datetime
    raw: dict[str, Any]                 # redacted/raw payload for diagnostics
```

### 2) Adapter interface

```python
class SurfaceAdapter(Protocol):
    surface_id: str
    mode: Literal["inline", "push"]

    async def normalize_inbound(self, raw_input: Any) -> SurfaceInboundEvent | None:
        """Validate + normalize transport payload. Return None for ignorable payloads."""

    async def resolve_owner_id(self, event: SurfaceInboundEvent, db: Session) -> int | None:
        """Map surface actor/conversation to Longhouse owner."""

    async def deliver(self, *, owner_id: int, text: str, event: SurfaceInboundEvent) -> None:
        """Deliver assistant response for push surfaces. Inline surfaces may no-op."""
```

### 3) Orchestrator contract

```python
class SurfaceOrchestrator:
    async def handle_inbound(self, adapter: SurfaceAdapter, raw_input: Any) -> SurfaceHandleResult:
        ...
```

Responsibilities:
- call `normalize_inbound()`
- resolve owner
- claim idempotency
- use existing `OikosService` per-owner run lock in phase 1
- call `OikosService.run_oikos(...)` with surface context
- call adapter `deliver(...)` when mode is `push`

## Strict Behavior Rules (No Fallback)

1. `dedupe_key` is required for every adapter ingress event.
2. Missing identity fields required by an adapter => drop/reject explicitly.
3. Idempotency lookup/claim failure => fail closed (no run spawn).
4. Unknown/unregistered surface IDs => hard reject.
5. Delivery errors are explicit failures; no implicit mirroring to other surfaces.
6. Push delivery retries are not automatic in v1; failures are logged and surfaced.

## Idempotency and Concurrency

Current Telegram dedupe scans recent thread messages. For multi-surface scale, move to a dedicated claim ledger.

### Proposed table

```sql
CREATE TABLE surface_ingress_claims (
  id INTEGER PRIMARY KEY,
  owner_id INTEGER NOT NULL,
  surface_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  source_event_id TEXT,
  source_message_id TEXT,
  claimed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(owner_id, surface_id, dedupe_key)
);
```

### Claim semantics

- Insert succeeds => first-seen ingress, proceed.
- Unique conflict => duplicate retry, acknowledge and stop.
- DB error => fail closed.

Keep existing per-owner serialization lock in `OikosService` for now; move lock to orchestrator once all entrypoints migrate.

## Adapter Mapping for Existing Surfaces

### Web adapter

- Raw input: `OikosChatRequest`
- `conversation_id`: `web:main`
- `dedupe_key`: `web:{owner_id}:{message_id}`
- `message_id` is required (client-generated UUID). Missing/invalid => reject.
- `mode`: `inline` (SSE response path already active)
- Delivery: no push action (response already in stream)

### Telegram adapter

- Raw input: `ChannelMessageEvent`
- `conversation_id`: `telegram:{chat_id}`
- `dedupe_key`: `telegram:{chat_id}:{update_id}` (required)
- `mode`: `push`
- Delivery: `TelegramChannel.send_message(...)`

### Voice adapter

- Raw input: turn-based voice request payload
- `conversation_id`: `voice:default` (or session-specific ID when available)
- `dedupe_key`: `voice:{session_id}:{turn_id}`
- `mode`: `inline` (or `push` if asynchronous voice callback path is introduced)

## Relationship to Existing Channel Plugin System

- Keep `zerg/channels/*` for transport mechanics (webhook, parsing, send).
- `SurfaceAdapter` is an Oikos-facing orchestration contract.
- For channel-based surfaces (telegram, future whatsapp/sms), adapters compose `ChannelPlugin` instances.
- Do not collapse `ChannelPlugin` and `SurfaceAdapter`; they solve different layers.

## Proposed File Layout

```text
apps/zerg/backend/zerg/surfaces/
  base.py                 # SurfaceInboundEvent, SurfaceAdapter protocol
  orchestrator.py         # shared ingress->oikos flow
  idempotency.py          # claim store abstraction
  registry.py             # adapter registry and lookup
  adapters/
    web.py
    telegram.py
    voice.py
```

Migration touchpoints:
- `routers/oikos_chat.py` -> delegate to `WebSurfaceAdapter`
- `services/telegram_bridge.py` -> replace with `TelegramSurfaceAdapter` + orchestrator
- `voice/turn_based.py` -> delegate to `VoiceSurfaceAdapter`
- `main.py` startup -> register adapters + wire registry

## Testing Strategy

### Contract tests (shared across adapters)

- valid ingress -> normalized event
- missing required identity -> rejected
- duplicate claim -> no Oikos run
- idempotency store error -> fail closed
- unknown/unregistered surface -> hard reject before orchestration
- metadata persisted (`origin`, `delivery`, `visibility`, ids)

### Adapter tests

- web adapter: message_id contract, owner mapping
- telegram adapter: strict `update_id` handling + chat resolution + push delivery
- voice adapter: turn identity contract and normalization

### Integration tests

- orchestrator + OikosService + SQLite claim table
- telegram webhook retry scenario (same update twice)
- concurrent web+telegram ingress serialized per owner
- registry dispatch path rejects unknown surface IDs

### E2E tests

- web-only history default
- all-activity mode shows cross-surface badges
- telegram inbound/outbound loop remains functional through orchestrator path

## Rollout Plan

### Phase 1: Introduce adapter skeleton behind existing behavior

- Add `surfaces/` package and orchestrator
- Implement adapters but keep legacy paths active
- Add contract tests first

### Phase 2: Cut over entrypoints

- web chat endpoint uses web adapter + orchestrator
- telegram bridge uses telegram adapter + orchestrator
- voice turn path uses voice adapter + orchestrator

### Phase 3: Remove legacy bridge-specific orchestration

- delete duplicate ingress logic from `TelegramBridge`
- keep transport plugin responsibilities only
- ensure one canonical ingress path for all surfaces

## Acceptance Criteria

1. Adding a new surface requires: adapter file + registration + tests (no Oikos core edits).
2. All inbound paths use shared idempotency claim flow.
3. All inbound paths use shared per-owner serialization flow (phase 1 via existing `OikosService` lock).
4. Surface metadata remains consistent across all entrypoints.
5. Existing web + telegram + voice behavior remains unchanged from user perspective.
6. Unknown surface IDs are rejected before run spawn.
7. Missing required ingress identity fields fail closed (no fallback path).

## Open Decisions

1. Whether to move per-owner lock fully out of `OikosService` into orchestrator in first cut or second cut.
2. Whether to support multiple web conversations (`web:<thread_id>`) now or keep `web:main` until product needs it.
3. Whether proactive tool sends (`send_telegram`) should persist explicit assistant timeline rows in v1 of this extraction.
