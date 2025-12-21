> **Note (Dec 19, 2025):** This doc is historical and reflects a local-first/IndexedDB-centric Jarvis direction.
> Current direction is Supervisor SSOT + all tools, with Realtime optional I/O-only:
> `docs/specs/jarvis-supervisor-unification-v2.1.md`

Architecture Alignment – Jarvis (Sept 13, 2025)

Status summary

- Client: IndexedDB stores for conversations, turns, documents exist. Added kv and outbox stores (v3) to support local-first + sync.
- Speed: In-memory MemoryVectorStore loads from IndexedDB and provides fast text search; ready for embeddings later.
- Engine: SessionManager (packages/core) now serializes writes via a queue and provides flush() for deterministic barriers.
- Shared packages: Extracted voice engine and local data layers into packages/core and packages/data/local for cross-surface reuse.
- Sync: Added sync scaffolding.
  - Client: outbox ops (append_turn) are queued; syncNow() pushes to server then pulls remote ops.
  - Server: sync endpoints not yet implemented on zerg-backend (TODO).
- Privacy: No E2E yet; token logging removed from client.
- Native: Electron shell loads PWA; no Keychain/E2E yet.

Data model (client)

- conversations(id, name, createdAt, updatedAt)
- turns(id, conversationId, timestamp, userTranscript?, assistantResponse?)
- documents(id, content, embedding[], metadata)
- kv(key, value)
- outbox(opId, deviceId, type, body, lamport, ts)

API (zerg-backend at /api/jarvis/\*)

- GET /session – OpenAI Realtime session token minting (implemented)
- POST /session – Same as GET (implemented)
- POST /conversation/title – Generate conversation title from messages (implemented)
- POST /sync/push – not yet implemented; idempotent by opId; returns { acked, nextCursor }
- GET /sync/pull?cursor – not yet implemented; returns { ops, nextCursor }

What’s implemented vs. the proposal

- Local-first + offline: Yes for capture/search; ask (LLM) still requires network.
- IndexedDB + MemoryVectorStore: Yes; VectorStore warms at session init.
- Append-only turns: Locally enforced; ops emitted as append_turn.
- LWW with Lamport: Lamport clock maintained client-side and stamped on ops, but conflict resolution is not yet exercised (single-user, dev server).
- flush() and syncNow(): Implemented on SessionManager.
- Privacy modes: Not yet; E2E remains future work.
- Sync service + Postgres: Server uses in-memory log for now; Postgres/pgvector are future work.

Performance targets (observability todo)

- Add simple metrics (write latency, outbox depth, last sync age) to logs and a debug panel later.

Phased rollout mapping

- Phase 1 (local determinism):
  - DONE: write queue + flush(); stop token logs; IndexedDB deterministic writes.
- Phase 2 (sync foundation):
  - DONE (scaffold): client outbox + syncNow(); server /sync push/pull.
  - TODO: Persist server state in Postgres; add cursors per device; durable op log table with opId uniqueness.
- Phase 3 (multi-device + UX):
  - TODO: pinning, last_active kv across devices, export/import in UI, deletes/tombstones.
- Phase 4 (privacy/perf):
  - TODO: E2E encryption, transcript virtualization, embeddings/pgvector.
- Phase 5 (native):
  - TODO: iOS app; Keychain key storage.

Notes

- Service worker currently caches legacy paths (realtime.js). Update for Vite build when moving to production.
- Conversation objects still carry a turns[] field in the schema comment; turns are stored separately; consider removing turns[] from conversation value to avoid duplication.
