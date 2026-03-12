# Oikos Conversations and Email Surface Tasks

- [x] Phase 0: Create persistent spec and tracking doc
- [x] Phase 1: Add `Conversation`, `ConversationBinding`, and `ConversationMessage` models
- [x] Phase 1: Add `ConversationService` with binding, append, list, and search helpers
- [x] Phase 1: Add SQLite-backed backend tests for conversation foundation
- [x] Phase 2: Add authenticated conversation list/read/search APIs
- [x] Phase 2: Add API tests for owner scoping and message retrieval
- [x] Phase 3 groundwork: Add provider-neutral email ingest service
- [x] Phase 3 groundwork: Add raw email archive store under `settings.data_dir / conversations`
- [ ] Phase 3: Ingest inbound email into conversations using existing email connectors
- [ ] Phase 4: Add Oikos conversation search/read/reply tools
- [ ] Phase 5: Migrate web and Telegram surfaces onto the conversation layer
