# Oikos Conversations and Email Surface Tasks

- [x] Phase 0: Create persistent spec and tracking doc
- [x] Phase 1: Add `Conversation`, `ConversationBinding`, and `ConversationMessage` models
- [x] Phase 1: Add `ConversationService` with binding, append, list, and search helpers
- [x] Phase 1: Add SQLite-backed backend tests for conversation foundation
- [x] Phase 2: Add authenticated conversation list/read/search APIs
- [x] Phase 2: Add API tests for owner scoping and message retrieval
- [x] Phase 3 groundwork: Add provider-neutral email ingest service
- [x] Phase 3 groundwork: Add raw email archive store under `settings.data_dir / conversations`
- [x] Phase 3: Ingest inbound email into conversations using existing email connectors
- [x] Phase 4a: Add Oikos `search_conversations` and `read_conversation` tools
- [x] Phase 4b: Add Oikos `list_conversations` and `reply_in_conversation` tools
- [x] Phase 5a: Add Gmail reply service for existing conversations only
- [x] Phase 5b: Append successful outbound replies back into the same conversation
- [x] Phase 5c: Add backend/tool tests for reply threading, recipient safety, and replay behavior
- [x] Phase 6a: Add canonical `POST /conversations/{id}/reply` backend endpoint
- [x] Phase 6b: Add inbox/thread UI backed by `/conversations`
- [x] Phase 6c: Keep `/api/oikos/conversations` as a deprecated façade until first-party migration is complete
- [x] Phase 7a.1: Create a stable canonical web conversation binding for `web:main`
- [x] Phase 7a.1: Expose canonical web conversation metadata through the existing Oikos bootstrap/thread path
- [x] Phase 7a.1: Mirror newly created web turns into `Conversation*` while keeping `/api/oikos/history` as the read path
- [x] Phase 7a.1: Add regression coverage for stable web binding reuse and mirrored writes
- [x] Phase 7a.2: Switch the default web Oikos read path from `/api/oikos/history` to canonical conversation reads
- [x] Phase 7a.2: Keep legacy `?thread=` prehydration as explicit compatibility behavior only
- [x] Phase 7a.3: Remove default web transcript dependence on the shared Oikos `SUPER` thread
- [x] Phase 7b.1: Map Telegram DMs to stable canonical conversations
- [x] Phase 7b.1: Map Telegram forum topics to stable canonical conversations with preserved topic metadata
- [x] Phase 7b.2: Mirror Telegram user/assistant turns into canonical conversations
- [x] Phase 7b.2: Add regression coverage for Telegram DM/topic binding identity and transcript writes
- [x] Phase 7c: Remove first-party dependence on `/api/oikos/history`
- [x] Phase 7c: Either delete `/api/oikos/history` or leave it compatibility/debug-only with reduced scope

## Done Conditions

### Phase 6c

- `/conversations` is treated as canonical in docs and first-party feature work
- `/api/oikos/conversations` is marked and documented as compatibility-only
- no new product surface work depends on the façade

### Phase 7a

- the web surface has one stable canonical conversation identity (`web:main`)
- new web turns are durable in `ConversationMessage`
- first-party web chat can reload from canonical conversations instead of fake
  surface-filtered Oikos thread history

### Phase 7b

- Telegram DMs and forum topics each map to their own durable canonical
  conversation
- Telegram transcript reconstruction no longer depends on the shared Oikos
  thread

### Phase 7c

- `/api/oikos/history` is no longer a first-party transcript API
- user-visible conversation lifecycle no longer depends on mutating the shared
  Oikos `SUPER` thread
