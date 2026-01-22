# Phase 2 – Robust parent-link & type-safe streaming state

This document explains **why** we need Phase 2, the design choices, and the
exact steps required to implement it.  It is meant for a **new engineer** who
just joined the project and has basic Rust + Python knowledge.

> _Reading time: ~10 minutes_

---

## 1 – Problem recap

The frontend renders chat history by receiving a WebSocket stream:
`StreamStart → *(assistant_token | assistant_message | tool_output)* → StreamEnd`.

### Current limitations after Phase 1 fix

| Area | Issue |
|------|-------|
| **Linking tool output** | In token-stream mode the server never ships a full `assistant_message`. The UI cannot know the **DB id** of the assistant row, so `tool_output` bubbles show as **orphans** instead of folding under the correct assistant bubble. |
| **Type-safety** | `state.active_streams: HashMap<u32, String>` stores `message_id` as an **arbitrary string**.  We `parse::<u32>()` on every access.  Bugs can hide here and we waste allocations. |

Phase 2 removes both issues while keeping backward-compat with non-token mode.


## 2 – High-level design

1. **Strongly-typed state** – change value type to `Option<u32>` (or an enum).
   *`None`* means _“stream started but assistant row id unknown yet”_.
2. **Assistant-ID frame** – backend broadcasts a tiny `{type:"assistant_id", data:{thread_id, message_id}}` frame **right after** it persists the assistant row when token streaming is on.
3. **Frontend flow**
   ```text
   StreamStart → active_streams[thread] = None
   assistant_token … (append) …
   AssistantId(thread, id) → active_streams[thread] = Some(id)
   tool_output … parent_id = active_streams[thread].unwrap_or(None)
   StreamEnd   → active_streams.remove(thread)
   ```

The change is additive; non-token mode keeps using the existing
`assistant_message` chunk which already carries its id.


## 3 – Task list (annotated)

### 3.1 Frontend (Rust)

| File | Task |
|------|------|
| `frontend/src/state.rs` | 1) Change field to `HashMap<u32, Option<u32>>`. <br>2) Add helper `fn current_assistant_id(&self, thread) -> Option<u32>` to centralise lookup. |
| `frontend/src/network/ws_schema.rs` | a) Add struct `WsAssistantId { thread_id: u32, message_id: u32 }`. <br>b) Extend `WsMessage` enum: `AssistantId(WsAssistantId)`. |
| `frontend/src/components/chat/ws_manager.rs` | Parse new variant and dispatch `Message::ReceiveAssistantId { thread_id, message_id }`. |
| `frontend/src/update.rs` | a) `ReceiveStreamStart` sets `active_streams.insert(thread, None)`. <br>b) Add handler for `ReceiveAssistantId` – overwrite to `Some(id)`. <br>c) In tool-output branch use `.flatten()` to fetch parent id. <br>d) Remove obsolete `parse::<u32>()` logic. <br>e) Clear map entry in `ReceiveStreamEnd`. |

### 3.2 Backend (Python)

| File | Task |
|------|------|
| `backend/zerg/schemas/ws_messages.py` | Add `class AssistantIdMessage(BaseModel)`.  Update `WsMessageType` enum if present. |
| `backend/zerg/routers/threads.py` **or** `AgentRunner.run_thread` | Immediately after inserting an assistant row _and_ if `runner.enable_token_stream` is **True**, broadcast `AssistantIdMessage`. |

> **Tip**: Re-use `topic_manager.broadcast_to_topic()` – no new infra needed.


## 4 – Testing strategy

### 4.1 Frontend (wasm-bindgen tests)

Create `frontend/tests/streaming.rs` with helper that feeds Msg sequence into
`update()` then inspects `AppState.thread_messages`.

Scenarios:

1. **Token mode**: `StreamStart → token x3 → AssistantId(42) → tool_output(parent)`
   * Expect exactly **two** bubbles (assistant + tool) and `tool.parent_id == 42`.
2. **Non-token mode**: `StreamStart → assistant_message(id=55) → tool_output(parent=55)`
   * Same assertion on `parent_id`.

### 4.2 Backend (pytest)

Add test in `backend/tests/test_ws_assistant_id.py`:

1. Start fake client subscribed to `thread:{id}`.
2. Run thread with `LLM_TOKEN_STREAM=True` env flag.
3. Assert an `assistant_id` frame arrives **before** first `tool_output`.


## 5 – Migration / Roll-out

1. Merge backend & frontend in the **same** release to avoid protocol skew.
2. Old clients will ignore the unknown `assistant_id` frame (safe). New
   clients connecting to old servers will *not* receive it; token-mode will
   still work (orphan UI) but without crashes.
3. Bump minor API version in `docs/websocket_streaming.md`.


## 6 – Gotchas & best-practices

* **Thread safety**: `active_streams` is mutated only inside the central
  `update()`; no extra `RefCell` juggling required.
* **Enum size**: Remember to add `#[derive(Clone, Debug, Deserialize)]` on new
  structs so Serde works in WASM.
* **Backend imports**: Keep new schema in the existing `ws_messages.py` to avoid
  circular imports.
* **Feature flag**: If environment variable `LLM_TOKEN_STREAM` is missing or
  `False`, backend must **NOT** send `assistant_id` (redundant); guard with
  simple `if`.
* **Performance**: The new frame is ~40 bytes – negligible.


## 7 – Estimated effort

| Task | Dev hrs |
|------|---------|
| Frontend refactor | 3 |
| Backend addition  | 1 |
| Tests (FE + BE)   | 2 |
| Docs & QA         | 1 |
| **Total**         | **7 hrs** |


## 8 – References & onboarding pointers

* Streaming spec: `docs/websocket_streaming.md` (update after merge).
* Frontend state machine: `frontend/src/update.rs` starting at `ReceiveStreamStart`.
* WebSocket schema: `frontend/src/network/ws_schema.rs` & `backend/zerg/schemas/ws_messages.py`.
* Token streaming callback: `backend/zerg/callbacks/token_stream.py`.


---

_Happy coding!  Reach out to **@core-frontend** or **@backend-ai** on Slack if you
get stuck._
