# Oikos Conversations and Email Surface

Status: proposed
Owner: David / Oikos
Updated: 2026-03-12

## Executive Summary

Longhouse currently has two different concepts mixed together:

- `Thread` / `ThreadMessage` as the persistence model for fiche execution
- Oikos as a single long-lived `SUPER` thread per user with surface-local metadata layered onto that one thread

That model was good enough for web + Telegram MVP work, but it is the wrong abstraction for a real assistant that participates in e-mail, chat, and future channels.

The product now needs a first-class **conversation** domain:

- every email chain is its own conversation
- every future Telegram topic / web chat / operator thread can also be its own conversation
- Oikos can search and read across conversations
- Oikos keeps its own private scratchpad thread, but human-visible communication stops pretending to be that scratchpad

This spec defines the new domain model, search/memory contract, mailbox onboarding strategy, and a phased rollout that keeps the existing Oikos stack working while we introduce conversations incrementally.

## Problem

Current behavior has three hard limits:

1. Human-visible history is not modeled as first-class conversations.
2. E-mail exists as connector-trigger plumbing, not as a durable assistant communication surface.
3. The user is still the message bus: email arrives, the user pastes it into a terminal/chat session, and the agent only then becomes aware of it.

That breaks the personal-assistant mental model. A real assistant should behave more like:

- I see my email threads
- my assistant can read and search them
- I can reply in Mail, web, or another surface and stay in the same thread
- the history is durable and searchable later

## Product Principles

- Email chains are their own threads.
- Human-visible conversations are first-class product objects.
- Oikos memory should query conversations, not hide them inside one giant private prompt transcript.
- Oikos may keep a private scratchpad, but that is not the user-facing source of truth.
- Search beats manual organization. Persist everything durable and let Oikos retrieve it.
- Existing mailbox onboarding should be easy for OSS users. Domain + MX + SES setup is not the default.
- Hosted low-cost users bring their own mailbox. Longhouse does not provision inboxes for them.
- Push/webhook richness is an upgrade, not a prerequisite for local installs.

## Non-Goals

- No attempt to make the current `Thread` table serve both fiche execution and human communication.
- No forced migration of all web/Telegram UI onto the new model in the first slice.
- No platform-provided agent mailboxes for the `$5/mo` hosted tier.
- No giant rules engine for every mail provider before the Gmail path works.
- No attempt to merge every historical transcript source into one schema before the inbox MVP exists.

## Current State

### Oikos

- Oikos has one persistent `SUPER` thread per user.
- Web, voice, operator, and Telegram all flow into that thread.
- Surface metadata is attached to `ThreadMessage.message_metadata`.
- `/api/oikos/history` can filter by surface for rendering, but the underlying memory remains one shared thread.

### Email

- Gmail connect exists and stores an email connector with refresh token + watch metadata.
- Gmail Pub/Sub and legacy webhook handlers process connector history and fire trigger jobs.
- Outbound email exists via SES helpers and Gmail send helpers.
- There is no first-class inbox, thread, message, or reply-by-email conversation model.

### Implication

We already have useful plumbing:

- connector auth/config
- Gmail history sync
- outbound threaded reply headers
- surface adapter/orchestrator infrastructure

But the canonical storage model for human communication does not exist yet.

## Decision Log

### Decision: Add a new `Conversation` domain instead of overloading `Thread`

**Context:** `Thread` is fiche execution state plus Oikos scratchpad state. Human communication has different semantics and product requirements.

**Choice:** Introduce `Conversation`, `ConversationMessage`, and provider bindings as a separate domain.

**Rationale:** This keeps human-visible communication durable and queryable without destabilizing fiche execution.

**Revisit if:** We later fully retire fiche `Thread` from user-visible features.

### Decision: Oikos keeps a private scratchpad for now

**Context:** The current Oikos runtime and tools are built around one `SUPER` thread.

**Choice:** Preserve the `SUPER` thread as Oikos private reasoning state in the near term.

**Rationale:** This makes the rollout additive and reversible. Conversations become first-class memory inputs before they become the entire Oikos runtime substrate.

**Revisit if:** A future Oikos runtime can operate directly on retrieved conversation context and ephemeral prompts.

### Decision: Reuse `Connector` for mailbox auth/config in v1

**Context:** Gmail connect, watch renewal, and refresh token storage already use `connectors`.

**Choice:** Keep mailbox credentials/config in `Connector` rows for the first rollout, rather than inventing a second auth table immediately.

**Rationale:** The conversation layer does not need a new mailbox credential model on day one.

**Revisit if:** We need multiple mailbox identities per provider per user or richer mailbox-local settings that no longer fit cleanly in `Connector.config`.

### Decision: Gmail and Microsoft-style OAuth are the default onboarding path; IMAP/SMTP is fallback

**Context:** Users want smooth onboarding, not mail-server configuration homework.

**Choice:** Design the product around provider-native mailbox connection first. Keep IMAP/SMTP as advanced fallback. Keep SES as advanced inbox provisioning infrastructure, not default onboarding.

**Rationale:** Existing mailbox connection is the smoothest path for both personal use and OSS adoption.

**Revisit if:** We choose a managed programmable mailbox product later for agent-owned inboxes.

### Decision: Use normalized DB text as the first searchable source of truth

**Context:** The user wants conversations persisted to disk and searchable. Full raw archive is valuable but adds ingestion complexity.

**Choice:** Store normalized conversation/message text in the DB immediately, with message-level raw archive references optional. Add raw `.eml` / payload archiving as a later additive phase.

**Rationale:** SQLite/Postgres persistence is already disk-backed for self-hosted installs, and DB search is enough to unlock Oikos retrieval and inbox UX quickly.

**Revisit if:** We need grep-friendly raw archives for all providers in the first inbox beta.

## Domain Model

### Conversation

Represents one human-visible thread.

Initial fields:

- `id`
- `owner_id`
- `kind` (`email`, `telegram`, `web`, `voice`, `operator`, `other`)
- `status` (`active`, `archived`, `closed`, `snoozed`)
- `title`
- `latest_message_at`
- `conversation_metadata`
- `created_at`
- `updated_at`
- optional `archived_at`

### ConversationMessage

Represents one normalized message inside a conversation.

Initial fields:

- `id`
- `conversation_id`
- `role` (`user`, `assistant`, `system`, `tool`, `external`)
- `direction` (`inbound`, `outbound`, `internal`)
- `author_name`
- `author_address`
- `content_text`
- optional `content_html`
- `occurred_at`
- optional `parent_message_id`
- optional `transport_message_id`
- optional `raw_source_path`
- `message_metadata`
- `created_at`
- `updated_at`

### ConversationBinding

Maps one internal conversation to one external surface thread identity.

Initial fields:

- `id`
- `conversation_id`
- `owner_id`
- optional `connector_id`
- `surface_id`
- `provider`
- `external_conversation_id`
- `binding_metadata`
- `created_at`
- `updated_at`

Example bindings:

- Gmail thread id
- Microsoft Graph conversation id
- Telegram `chat_id:topic_id`
- web conversation id

### ConversationMessageBinding

Maps one normalized message to one external provider message identity for dedupe and reply threading.

Initial fields:

- `id`
- `conversation_message_id`
- optional `connector_id`
- `surface_id`
- `provider`
- `external_message_id`
- `binding_metadata`
- `created_at`

This is how we avoid re-importing the same Gmail or Telegram message on webhook retry or poll replay.

## Search and Memory Model

### Canonical truth

The canonical store for human-visible assistant communication becomes:

- DB rows in `conversations` and `conversation_messages`
- optionally linked raw archives on disk

### Oikos retrieval

Oikos gains tools and services to:

- search conversations by query / surface / participant / recency
- read a conversation thread
- reply in an existing conversation

The private Oikos `SUPER` thread remains available for short-lived working memory, but long-lived human communication is no longer stored there as the only truth.

### Search rollout

Phase 1:

- simple DB-backed search over normalized message text

Phase 2:

- SQLite FTS5 for local installs
- Postgres-native FTS where applicable

Phase 3:

- optional raw archive references for exact payload inspection / future grep workflows

## Surface Model

The current `SurfaceAdapter` layer stays useful, but its role changes.

Instead of:

- inbound surface event
- directly run Oikos against the shared `SUPER` thread
- optionally annotate shared thread messages

the target shape becomes:

- inbound surface event
- resolve/create canonical conversation
- persist normalized conversation message
- optionally wake Oikos with the canonical conversation id
- deliver assistant response back through the same surface

This lets email, Telegram, web, and future channels share the same product model without forcing the same transport semantics.

## Email Model

### Inbound

For provider-backed mailboxes:

- connect mailbox auth/config through `Connector`
- ingest provider thread + message identity
- upsert `ConversationBinding`
- append normalized `ConversationMessage`
- bind provider message ids for dedupe

### Outbound

Reply behavior:

- default to replying inside an existing conversation
- preserve provider-native thread identity when supported
- preserve RFC threading headers when sending raw email
- first-contact or new-recipient outbound remains policy-gated

### Ownership

Each email conversation belongs to one Longhouse owner, but can contain many external participants.

Mailbox identity comes from the connector/account used to ingest or send.

## Onboarding Strategy

### Personal instance

- Gmail via OAuth first
- Gmail push/watch if infra exists
- polling fallback when push is not available
- advanced later: custom subdomain / SES / programmable inboxes

### OSS self-hosted

Recommended order:

1. Connect Gmail
2. Connect Microsoft 365 / Outlook
3. Other mailbox via IMAP/SMTP (advanced)
4. Programmable inbox/domain setup (advanced)

Principles:

- Do not require SES or domain setup for first mailbox onboarding.
- Do not require a public webhook endpoint for the first working inbox.
- Polling is acceptable for the first OSS experience.

### Hosted `$5/mo` tier

- user brings their own mailbox
- Longhouse provides mailbox connection UX
- Longhouse does not provide inbox hosting

## UX Target

The end-state UX should feel like a real assistant inbox:

- inbox list
- searchable threads
- thread detail view
- reply from web
- reply from native mail client
- same underlying conversation either way

The inbox is not an alert dump. It is the assistant’s conversation workspace with the human.

## Architecture Ownership

### Backend models and services

New likely ownership areas:

- `apps/zerg/backend/zerg/models/conversation.py`
- `apps/zerg/backend/zerg/services/conversation_service.py`
- `apps/zerg/backend/zerg/schemas/conversation_schemas.py`
- `apps/zerg/backend/zerg/routers/conversations.py`

Existing files likely to evolve:

- `apps/zerg/backend/zerg/database.py`
- `apps/zerg/backend/zerg/models/__init__.py`
- `apps/zerg/backend/zerg/services/oikos_service.py`
- `apps/zerg/backend/zerg/surfaces/orchestrator.py`
- `apps/zerg/backend/zerg/routers/auth.py`
- `apps/zerg/backend/zerg/routers/email_webhooks_pubsub.py`
- `apps/zerg/backend/zerg/email/providers.py`
- `apps/zerg/backend/zerg/shared/email.py`
- `apps/zerg/backend/zerg/services/gmail_api.py`

### Frontend

Likely new frontend areas:

- inbox page
- thread detail page
- mailbox onboarding settings
- future cross-surface conversation search surfaces

Existing likely touch points:

- `apps/zerg/frontend-web/src/pages/IntegrationsPage.tsx`
- new conversation/inbox API hooks and pages

## Phased Implementation

### Phase 0: Spec and tracking

- create persistent spec
- create tracking doc
- record rollout notes in `TODO.md`

Acceptance:

- spec is concrete enough to drive the first code slice
- decisions are recorded instead of deferred into chat

### Phase 1: Conversation foundation

- add `Conversation`, `ConversationMessage`, `ConversationBinding`, `ConversationMessageBinding`
- add startup-safe lightweight SQLite migrations
- add service helpers to create conversations, append messages, and resolve bindings
- add minimal authenticated read APIs:
  - list conversations
  - get conversation detail
  - get messages
  - basic search
- add backend tests for binding resolution, append ordering, and search

Acceptance:

- Longhouse can persist and query first-class conversations independent of fiche `Thread`
- provider/surface bindings can map inbound external thread ids to one canonical conversation
- search can find conversation messages without touching Oikos `SUPER` history

### Phase 2: Email conversation ingest MVP

- add email ingest path that writes conversations instead of only firing triggers
- support Gmail first using existing connector + history sync plumbing
- create/update email conversation bindings from Gmail `threadId`
- store normalized messages and dedupe by provider message id
- add reply service for existing email conversations

Acceptance:

- a connected Gmail account can produce canonical conversations and messages
- reprocessing the same Gmail history does not duplicate messages
- assistant replies can be sent back in-thread

### Phase 3: Inbox UI and mailbox onboarding

- add web inbox list + thread view
- expose mailbox connection status and email-specific onboarding on settings/integrations
- support provider-first onboarding copy and advanced fallback entry points

Acceptance:

- a user can browse and search email conversations in Longhouse
- a user can reply from web in the same thread
- onboarding copy does not force SES/SMTP setup for common cases

### Phase 4: Oikos conversation retrieval tools

- add search/read/reply conversation tools for Oikos
- teach Oikos to reference conversations as durable memory
- add policy around reply-only vs first-contact sends

Acceptance:

- Oikos can search and read prior email threads without manual paste-in
- Oikos can draft or send replies within policy bounds

### Phase 5: Converge other surfaces onto conversations

- move Telegram topics and future web chat threads onto the same conversation domain
- keep the current `SUPER` thread as private scratchpad until replacement is justified

Acceptance:

- web, Telegram, and email can all participate in the same product-level conversation model
- human-visible history is no longer stored only as surface-filtered views of a private scratchpad

## Testing Strategy

### Backend tests

- conversation creation / append / list ordering
- binding uniqueness and lookup
- message dedupe through message bindings
- search results over normalized text
- Gmail ingest replay idempotency
- reply threading metadata preservation

### Browser tests

- inbox list renders seeded conversations
- thread detail loads messages in order
- search returns expected thread
- reply action posts into the same conversation

### Live QA

- connect a real Gmail mailbox on a dev instance
- ingest a known existing thread
- verify thread appears once with correct messages
- reply from web and confirm native mailbox threading
- reply from native mail client and confirm Longhouse appends to the same thread

## Acceptance Criteria

- Email chains are modeled as first-class conversations, not stuffed into the Oikos `SUPER` thread.
- Oikos can search and read durable conversations without the human manually pasting e-mail into a coding session.
- The first mailbox onboarding path is smooth for common users and does not require SES/domain setup.
- Hosted cheap users can connect their own mailbox, but Longhouse does not need to provide inboxes.
- The rollout is additive: current Oikos behavior keeps working while conversations land in phases.

## Immediate Next Step

Build Phase 1 only:

- conversation models
- service layer
- minimal authenticated APIs
- tests

Do not begin Gmail ingest cutover until that foundation exists and passes its own test ring.
