# WebSocket Schema Refactor – Developer On-Ramp

> **Status** – Phase 1 (core plumbing) **DONE**, incremental migrations pending.
> Last updated: 2025-05-01

---

## 0. TL;DR for the Next Engineer 🔑

The frontend has switched from *stringly-typed* JSON handling to a **typed
WebSocket schema** defined in `frontend/src/network/ws_schema.rs`.

* `WsMessage` (an enum) represents every frame the backend can send.
* `WsMessage::topic()` tells us which UI topic (`agent:{id}`, `thread:{id}` …)
  the message belongs to.
* `TopicManager` and `DashboardWsManager` are already migrated to the new API.
* Legacy code still works (fallback logic) – remaining consumers can be
  migrated gradually.

---

## 1. Where We Came From

### Old Flow (pre-refactor)

```text
raw JSON (serde_json::Value)
    ↓  // manual field poking everywhere
if value["type"] == "run_update" { … }
// 30-line match blocks sprinkled across the codebase
```

Drawbacks

* Duplicate parsing logic → bugs / drift.
* Easy to misspell a key; failures are silent.
* Console spammed with *Unknown message type* warnings.

### Trigger for Change

While adding run-history and new event types, the match block in
`TopicManager` ballooned and logs got noisy (`agent_state`, `run_update` …).
We chose **Option 2** (typed schema layer) from the earlier design doc.

---

## 2. What Changed (Phase 1)

### 2.1  New file – `frontend/src/network/ws_schema.rs`

* `enum WsMessage` – discriminated union (`#[serde(tag="type")]`).
  * Variants: `RunUpdate`, `AgentEvent`, `ThreadEvent`, `StreamStart`,
    `StreamChunk`, `StreamEnd`, `Unknown`.
  * Accepts legacy aliases (`agent_state`, `agent_created`, …) via
    `#[serde(alias = "…")]`.
* Payload structs: `WsRunUpdate`, `WsAgentEvent`, `WsThreadEvent`, … – think
  Pydantic `BaseModel`s.
* `impl From<WsRunUpdate> for ApiAgentRun` converts slim socket payload → rich
  REST model.
* `WsMessage::topic()` centralises routing.

### 2.2  `frontend/src/network/topic_manager.rs`

Replaced 40-line manual topic-detection with:

```rust
match serde_json::from_value::<WsMessage>(msg) {
    Ok(parsed) => parsed.topic(),
    Err(_)     => None,   // admin frames
}
```

### 2.3  `frontend/src/components/dashboard/ws_manager.rs`

Uses `WsMessage` pattern-matching; no JSON digging.

### 2.4  `frontend/src/network/messages.rs`

Generic fallback parser now ignores any `type` that starts with `agent_` to
silence redundant warnings.

### 2.5  Compatibility tweaks

* Legacy strings (`agent_state`, etc.) accepted via `alias` attributes.
* Build passes (`cargo check`).  Only unrelated dead-code warnings remain.

---

## 3. Remaining Work (Phase 2+) 🗺️

| Task | Owner | Size | Notes |
|------|-------|------|-------|
| Migrate **chat/stream** handlers to `WsMessage::{StreamStart, …}` | FE | ✅ | ChatViewWsManager migrated – fully typed. |
| Migrate **analytics panel** & other WS consumers | FE | ⏳ | Dashboard is typed; analytics stubs still pending. |
| Add wasm-bindgen unit tests for
  · `WsRunUpdate → ApiAgentRun`
  · `WsMessage::topic()` | FE | S | Use `wasm_bindgen_test`. |
| Delete legacy parsing when 100 % migrated | FE | ⏳ | Stream branches removed; ping/pong still use legacy parser. |
| (Optional) **Option 3** – shared Rust + Python IDL | BE+FE | L | Full schema sync, removes conversion layer. |

Search helpers:

```bash
# find remaining manual JSON poking
rg '\["type"\]' frontend/src | less
rg 'stream_chunk' frontend/src
```

---

## 4. Backend Frame Contract (reference)

```text
{ "type": <string>, "data": { … } }

run_update   → data.agent_id, data.status, …
agent_event  → data.id,       data.status?, data.next_run_at?, …
thread_event → data.thread_id, data.agent_id?, …
stream_start → thread_id
stream_chunk → thread_id, chunk_type, content?
stream_end   → thread_id
```

Anything not recognised becomes `WsMessage::Unknown` → logged once, ignored.

---

## 5. File / Function Cheat-Sheet

| Path | Role |
|------|------|
| `frontend/src/network/ws_schema.rs` | **Single source of truth** for WS payloads |
| `WsMessage::topic()` | Converts message → "agent:42" / "thread:7" |
| `TopicManager::route_incoming_message` | Central dispatcher; now uses schema layer |
| `DashboardWsManager::subscribe_to_agent_events` | Applies RunUpdate / AgentEvent to state |
| `backend/zerg/websocket/manager.py` | Emits the frames (unchanged) |

---

## 6. FAQ

**Is this like Pydantic?**
Yes – `serde` derives work like Pydantic models: automatic JSON → struct
validation.

**Do we *have* to migrate everything now?**
No – old code still works, but new features should prefer `WsMessage`; the old
helpers are effectively deprecated.

**What if the backend adds a new event?**
Add a variant + payload struct in `ws_schema.rs`, implement `topic()`.  The
compiler forces you to handle it anywhere you `match` on `WsMessage`.

---

*Happy hacking!* 🎉
