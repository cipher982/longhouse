# Oikos Conversations and Email Surface

Status: in progress
Owner: David / Oikos product direction
Updated: 2026-03-12

## Executive Summary

Longhouse needs a first-class conversation layer above the existing Oikos `SUPER` thread.

Today the product treats the per-user Oikos thread as both:

- Oikos's private working memory
- the human-visible conversation store

That was tolerable for a single web chat, but it breaks once email, Telegram, operator wakeups, and future surfaces all need their own durable threads.

The core change is:

- `Thread` / `ThreadMessage` remain Oikos-private execution memory for now
- new `Conversation` records become the canonical human-visible transcript layer
- every email chain is its own `Conversation`
- every surface event binds to a `Conversation`, not directly to the one Oikos thread
- Oikos searches/read-reconstructs from conversations and agent sessions instead of pretending one immortal prompt thread is the source of truth

This spec keeps the first implementation slice deliberately narrow:

- add additive conversation tables and services
- keep existing Oikos chat behavior working
- wire email into conversations before migrating web/Telegram

## Current Implementation Status

Completed locally in this session:

- Phase 0 spec and task tracker
- Phase 1 conversation tables and service helpers
- Phase 2 authenticated list/read/search/message APIs
- Phase 3 groundwork: provider-neutral email ingest + raw archive service
- Phase 3 inbound Gmail connector integration inside `GmailProvider.process_connector()`
- Phase 4 search/read groundwork: Oikos `search_conversations` and `read_conversation` tools
- targeted backend tests for conversation APIs, email ingest, and Gmail replay behavior

Still blocked:

- Oikos reply tooling and conversation-aware outbound email append
- migration of web and Telegram onto the new conversation domain

## Problem

Current state in Zerg:

- Oikos has exactly one long-lived `SUPER` thread per user
- surface adapters already pass `surface_id` and `conversation_id`
- those identifiers only annotate `ThreadMessage.message_metadata`
- `/api/oikos/history` filters the shared Oikos thread by surface metadata for presentation
- Gmail integration is connector/trigger oriented, not conversational

That creates five real problems:

1. Human-visible threads are not first-class data.
2. Email cannot work like a normal personal assistant inbox because email chains are not stored as their own conversations.
3. Oikos memory and human transcript concerns are coupled.
4. Search across "all my threads" is missing because the only durable chat store is the one Oikos thread.
5. OSS and hosted onboarding are muddy because email transport and conversation semantics are mixed together.

## Product Principles

### 1. Human-visible conversations are canonical

Humans should interact with conversations, not with Oikos's private execution thread.

Examples:

- an email chain is one conversation
- a Telegram DM is one conversation
- a Telegram forum topic is one conversation
- a web chat thread is one conversation

### 2. Oikos private memory stays separate

The Oikos `SUPER` thread is still useful as private scratch space, summaries, internal coordination, and execution history.

It is not the canonical user inbox or transcript model.

### 3. Email is a surface, not a workflow hack

Replying to an email should append to the same durable conversation that web and terminal tooling can inspect.

Email must not require:

- forwarding raw content into a fresh coding session
- copy/pasting alerts into the terminal
- creating a new orphaned agent session per reply

### 4. Search must span conversations and sessions

Oikos should be able to:

- list conversations
- search conversation content
- read a conversation
- search agent sessions separately
- combine evidence from both

### 5. OSS onboarding must stay practical

Longhouse should support user-provided mailboxes without Longhouse becoming a mailbox host.

The product should support:

- Gmail / Google Workspace as the preferred mailbox-connect path
- other providers later
- IMAP/SMTP as an advanced compatibility option, not the primary onboarding UX
- BYO mailbox for hosted low-cost instances

### 6. Raw email should be archived to disk

Normalized text belongs in the database.

Original transport artifacts also matter:

- raw `.eml`
- attachments
- provider-specific headers and threading data

Those should live under `settings.data_dir`, referenced from the DB, so they remain grep-able and recoverable.

## Non-Goals

- Do not replace the existing Oikos `SUPER` thread immediately.
- Do not migrate all web/Telegram history into conversations in the first slice.
- Do not build a full mailbox provisioning control plane.
- Do not make Longhouse responsible for hosting inboxes for low-cost hosted users.
- Do not overfit the domain model to email-only semantics.
- Do not introduce a giant rules engine for memory/thread routing.

## Current State

### What already exists

- Surface adapters normalize inbound events with `surface_id`, `conversation_id`, `dedupe_key`, and owner resolution.
- The shared `SurfaceOrchestrator` already handles transport-agnostic dedupe and Oikos invocation.
- Gmail connectors already persist provider config such as `history_id`, `watch_expiry`, and `emailAddress`.
- Gmail Pub/Sub plumbing already exists in the broader stack.
- Outbound email helpers already support proper reply headers.

### What is missing

- outbound in-thread reply append into conversations
- Oikos reply tooling on top of the conversation layer
- migration of web and Telegram onto canonical conversations
- a clean separation between Oikos-private memory and human-visible threads for every surface, not just email

## Domain Model

### Conversation

Canonical human-visible thread.

Proposed MVP fields:

- `id`
- `owner_id`
- `kind` (`email`, `telegram`, `web`, `voice`, `operator`, `system`)
- `title`
- `status` (`active`, `archived`, `spam`, `hidden`)
- `conversation_metadata` JSON
- `last_message_at`
- `created_at`
- `updated_at`

Notes:

- `kind` is the user-facing thread type, not necessarily the transport provider.
- `status` is deliberately lightweight for MVP.

### ConversationBinding

Maps one durable conversation to a surface-native thread key.

Proposed MVP fields:

- `id`
- `conversation_id`
- `owner_id`
- `surface_id`
- `provider`
- `binding_scope`
- `connector_id` nullable
- `external_conversation_id`
- `binding_metadata` JSON
- `created_at`
- `updated_at`

Examples:

- `surface_id=email`, `provider=gmail`, `binding_scope=connector:12`, `external_conversation_id=<gmail threadId>`
- `surface_id=telegram`, `external_conversation_id=telegram:<chat_id>`
- `surface_id=telegram`, `external_conversation_id=telegram:<chat_id>:topic:<topic_id>`
- `surface_id=web`, `external_conversation_id=<web conversation GUID>`

MVP uniqueness:

- one binding per `(owner_id, surface_id, provider, binding_scope, external_conversation_id)`

### ConversationMessage

Canonical message rows for the human-visible transcript.

Proposed MVP fields:

- `id`
- `conversation_id`
- `role` (`user`, `assistant`, `system`, `tool`)
- `direction` (`incoming`, `outgoing`, `internal`)
- `sender_kind` (`human`, `agent`, `tool`, `system`)
- `sender_display`
- `content`
- `content_blocks` JSON nullable
- `external_message_id` nullable
- `parent_message_id` nullable
- `archive_relpath` nullable
- `message_metadata` JSON
- `internal`
- `sent_at`

MVP uniqueness:

- one message per `(conversation_id, external_message_id)` when `external_message_id` is present

### Raw Archive

Disk layout under `settings.data_dir / "conversations"`:

- raw RFC822 mail
- attachment payloads
- provider-specific payload snapshots when useful

Archive policy:

- DB stores normalized message text and metadata
- DB stores `archive_relpath`
- archive files are append-only durable artifacts

## Key Decisions

### Decision: Keep Oikos `SUPER` thread as private memory for phase 1

**Context:** The current Oikos runtime, wakeup logic, and history endpoints all assume a single long-lived Oikos thread.

**Choice:** Do not replace it yet. Add conversations alongside it.

**Rationale:** This is the smallest reversible way to introduce the correct human-facing data model without breaking current Oikos execution.

**Revisit if:** Web and Telegram have both migrated onto the conversation layer and the old history endpoints are no longer primary.

### Decision: Reuse `Connector` for mailbox/provider auth

**Context:** Email providers already use `Connector(type="email", provider=...)` as the account/config store.

**Choice:** Do not invent a separate mailbox-account table in the MVP.

**Rationale:** Connector records already carry the right ownership and provider config semantics.

**Revisit if:** We later need one connector to expose multiple independently addressable mailboxes/personas.

### Decision: Email chains get their own conversations

**Context:** The human wants email to work like a normal assistant inbox, with durable searchable threads.

**Choice:** Each provider thread maps to one `Conversation`.

**Rationale:** This matches human expectations and keeps reply semantics simple.

**Revisit if:** A provider lacks a stable native thread concept and RFC822 fallback proves insufficient.

### Decision: Raw archive lives under `settings.data_dir`

**Context:** The user explicitly wants durable, grep-able mail history, not only normalized DB rows.

**Choice:** Store raw mail artifacts on disk and reference them from conversation messages.

**Rationale:** This fits Longhouse's existing artifact-first philosophy and OSS deployment model.

**Revisit if:** Attachment volume or multi-instance replication requires a dedicated object store abstraction.

### Decision: Gmail-first onboarding, provider-agnostic internals

**Context:** Smooth onboarding matters for OSS and hosted users, but Longhouse should not become a mailbox host.

**Choice:** Favor Gmail/Workspace first, keep connector/provider abstractions generic, and leave IMAP/SMTP as advanced fallback.

**Rationale:** This aligns with the user's desired UX without hardcoding one proprietary mail provider into the data model.

**Revisit if:** Microsoft 365 becomes equally common in the actual user base.

## Architecture

### Inbound flow

#### Email

1. Email transport receives a new message event.
2. The provider resolves the owning `Connector`.
3. The provider computes a stable `surface_id` + `external_conversation_id`.
4. `ConversationService` upserts the `ConversationBinding`.
5. Raw payload is archived to disk.
6. Normalized message is persisted to `ConversationMessage`.
7. Oikos may be woken with the conversation ID and compact context.
8. Any assistant reply is appended back into the same conversation.

#### Other surfaces

The same model should apply later:

1. normalize inbound transport event
2. resolve or create conversation binding
3. persist conversation message
4. optionally invoke Oikos
5. persist assistant response in the same conversation

### Oikos context model

Oikos should operate against:

- private Oikos thread for scratch context
- selected conversation transcript
- search over other conversations
- search over agent sessions

That means future Oikos tools should include:

- `list_conversations`
- `search_conversations`
- `read_conversation`
- `reply_in_conversation`

The Oikos runtime does not need every conversation in prompt context. It only needs retrieval and durable references.

### Search model

MVP search should be additive and SQLite-friendly.

Planned path:

- Phase 1: service-level search using indexed DB rows
- Phase 2: SQLite FTS-backed conversation message search
- Phase 3: unify conversation search with existing session-search UX

Search is a product requirement, but FTS-backed optimization does not need to block the first schema slice.

### API surface

MVP backend APIs:

- `GET /conversations`
- `GET /conversations/{id}`
- `GET /conversations/{id}/messages`
- `GET /conversations/search?q=...`
- temporary façade: `/api/oikos/conversations/*`

Email-specific APIs later:

- mailbox connect status
- mailbox sync state
- thread reply / draft endpoints

Current `/api/oikos/thread` and `/api/oikos/history` remain for compatibility until web chat migrates.

## Onboarding Model

### Personal instance

- Connect existing Gmail / Workspace mailbox first
- Later optionally add SES on `agents.drose.io` for headless agent personas

### OSS self-hosted

- preferred: connect Gmail / Workspace
- advanced fallback: other mailbox providers
- raw provider complexity stays behind connector setup and sync services

### Hosted low-cost users

- BYO mailbox only
- no Longhouse-hosted mailbox provisioning
- connect existing account, then use Longhouse as assistant UI + automation layer

## Implementation Phases

### Phase 0: Spec and task tracker

- create this spec
- record decisions and phase boundaries

Acceptance criteria:

- persistent spec exists in-repo
- phase sequencing is explicit
- blockers and non-goals are written down

### Phase 1: Additive conversation foundation

- add `Conversation`, `ConversationBinding`, `ConversationMessage` models
- register them in startup DB initialization
- add `ConversationService`
- add backend tests for create/bind/append/search behavior

Acceptance criteria:

- new tables create cleanly in SQLite
- bindings dedupe by `(owner_id, surface_id, external_conversation_id)`
- message append updates `last_message_at`
- existing Oikos/thread behavior remains unchanged

### Phase 2: Conversation APIs

- add list/read/search message APIs for authenticated owners
- make `/conversations` the canonical API surface
- keep `/api/oikos/conversations` only as a temporary façade if needed
- add API tests

Acceptance criteria:

- authenticated user can list own conversations
- authenticated user can read one conversation and its messages
- search returns matching conversations without leaking other users' data

### Phase 3: Email conversation ingestion

- add an email conversation ingress service using existing email `Connector` records
- map provider thread IDs to `ConversationBinding`
- archive raw email to disk
- persist normalized inbound messages
- add tests with mocked provider responses

Acceptance criteria:

- a new inbound email creates a conversation and message rows
- a reply on the same provider thread reuses the same conversation
- raw archive path is stored for each ingested message

### Phase 4: Oikos reads and replies through conversations

- add Oikos tools for conversation search/read/reply
- make email reply path append assistant output into the same conversation
- preserve Oikos private thread for scratch summaries and internal coordination

Acceptance criteria:

- Oikos can search conversation history without depending on the shared Oikos chat transcript
- assistant replies remain attached to the correct email conversation

### Phase 5: Migrate web and Telegram onto conversations

- web chat writes to conversations
- Telegram chat/topic writes to conversations
- `/api/oikos/history` becomes compatibility-only or is retired

Acceptance criteria:

- web and Telegram have their own first-class conversations
- the old surface-filtered shared-thread model is no longer primary

## Testing Strategy

### Backend unit tests

- conversation binding dedupe
- message append updates timestamps
- duplicate external message IDs do not create duplicate rows
- per-owner search isolation
- conversation APIs enforce owner scoping

### Transport integration tests

- inbound email creates conversation + message rows
- same thread reuses existing conversation
- assistant reply writes outbound conversation message

### Live QA targets

- connect a Gmail mailbox
- receive a new thread
- verify it appears as one conversation
- reply by email client
- verify the same conversation updates
- ask Oikos about that thread and verify retrieval works

## Acceptance Criteria

- Email chains are first-class conversations, not annotations on the shared Oikos thread.
- Oikos can search across conversations and agent sessions as separate evidence stores.
- The system persists normalized conversation rows in DB and raw email artifacts on disk.
- The first release works for BYO mailbox setups and does not require Longhouse-hosted mailboxes.
- Existing Oikos web behavior keeps working while the migration is in progress.

## Open Issues

- `/conversations` is the intended canonical read API, but `/api/oikos/conversations` still exists as a temporary façade until the client migration decision is finalized.
- Gmail inbound is now conversation-aware, but outbound in-thread reply append is still missing.
- Telegram topic/reply metadata is still not preserved end-to-end; that should be fixed during its migration phase rather than ignored.
