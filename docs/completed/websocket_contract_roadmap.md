# WebSocket Contract Hardening — Road-map

Single source-of-truth = **`asyncapi/chat.yml`**.
Everything else (code-gen, validation, tests, CI gates) grows out of that one
file. The tasks below are grouped in successive, _mergeable_ phases; each
brings immediate value and leaves the codebase in a green state.

---

## Phase 1 — AsyncAPI spec & code-generation

### 1.1 Draft `asyncapi/chat.yml` (✅ once merged)

- [x] Describe global info (`title`, `version`, etc.)
- [x] Channels
  - [x] `system` – control frames (subscribe / unsubscribe / error / …)
  - [x] `thread:{thread_id}` – history, stream*start/stream_chunk/stream_end *(delimiter switched to `:` to match runtime topics)\_
  - [x] `agent:{agent_id}` – agent_event
- [x] Components / schemas for every payload type (Ping, Pong, Error,
      ThreadHistory **+ new SendMessage**)
- [x] Example messages for each operation (covering ping/pong, stream,
      agent_event, etc.)

### 1.2 Validation in CI

- [x] Add `npx @asyncapi/cli validate asyncapi/chat.yml` (with legacy fallback) to **pre-commit hook**
- [x] `scripts/validate-asyncapi.sh` runs as first step of `make test` so CI
      fails on invalid spec.

### 1.3 Code-generation targets

- [x] Backend (Rust):
  - output dir `backend/zerg/ws_schema/` (temporary, not part of git)
  - command: uses `asyncapi-rust-ws-template` (community-maintained, see npm/github: kanekoshoyu/asyncapi-rust-ws-template)
  - template can be overridden via `ASYNCAPI_RUST_TEMPLATE` env var for vendoring or pinning.
- [x] Frontend (TypeScript → wasm-bindgen):
  - output dir `frontend/generated/`
  - command: uses `modelina` for TypeScript model generation.
- [x] Script `scripts/regen-ws-code.sh` invoked by `make regen-ws-code`
  - script is robust to template/network issues, supports parameterization, and is documented.
- [x] Node version pinned via `.nvmrc` (Node 20).
- [x] CI job calling `make ws-code-diff-check` to enforce no drift
- [x] Makefile target `ws-code-diff-check` added (runs regen + git diff)
- [x] Lessons learned: prefer HTTPS for git/npm, pin tool versions, and document all changes.

---

## Phase 2 — Runtime payload validation

### 2.1 Backend

- n/a `schemars::JsonSchema` (backend is Python; Rust code is client SDK only)
- [x] Central validation in WS handler (schema map + 1002 close)
- [x] Heart-beat watchdog – server pings every 30 s, disconnects after 60 s without _pong_ (code **4408**)

### 2.2 Frontend

- [x] Full JSON-Schema validation of incoming frames via Rust `jsonschema` crate _(Jun 2025 switch from planned `ajv8` to all-Rust solution; 0.17, draft-2020-12)_
- [x] Lightweight envelope sanity-check removed (superseded by full schema) – still O(µs) overhead.
- [x] Auto-reply with `pong` when server sends `ping`.
- [x] Toast notifications for close codes **1002** and **4408**.
- [x] WS badge colour-coding (green/amber/red) implemented – inline `<span id="ws-badge">` driven by `ui_updates::update_connection_status()`.

> Note: original plan mentioned _ajv8_; we decided to keep the stack Rust-only to
> reduce JS-WASM boundary complexity and bundle size. The docs and CI scripts
> have been updated accordingly.

---

## Phase 3 — Consumer-driven contract tests (Pact V4)

### 3.1 Front-end (consumer) — _pure Rust/WASM_

- [x] Stand-alone binary (`frontend/src/bin/contract_capture.rs`) writes deterministic Pact JSON.
  - subscribe_thread → expect **ThreadHistory**
  - send_message → expect **stream_start / stream_chunk / stream_end**
- [x] Serialises to Pact V4 JSON and writes `contracts/frontend-v1.json` (committed).

### 3.2 Back-end (provider) — _Python pact-verifier_

- [x] Lightweight PyPI dep will be optional; test skips when absent.
- [x] New pytest `test_pact_contracts.py` boots FastAPI via TestClient and verifies every JSON in `contracts/`.

### 3.3 CI integration

- [x] `make pact-capture` – runs the capture binary, fails on diff.
- [x] `make pact-verify` – executes provider verification tests.

---

## Phase 4 — Property-based fuzzing

- [x] Generate proptest strategies from JSON Schema (blog pattern)
- [x] Fuzz sequences: random valid control & user messages (max 30)
- [x] Assert: server never panics, always responds within 100 ms

---

## Phase 5 — Fail-fast UX polish

- [x] Ping watchdog on frontend: if no frame for (`ping_interval × 2`) → force reconnect (implemented in `WsClientV2`).
- [x] Error toast & WS badge colour coding for close codes 1002, 4401, 4408 (toasts landed earlier; badge colours now update).

---

### Lessons learned so far (Jul 2025)

• Matching channel addresses to the runtime `thread:{id}` / `agent:{id}` early
prevented SDK contract drift.
• A single–sided heartbeat (server → ping, client → pong) is simpler than
dual-timers and plays nicer with multiple open tabs.
• Keeping one central Pydantic schema map (`websocket/handlers.py`) makes it
trivial to add validation for new message types.
• The generated Rust artefacts are for _client_ SDKs; `schemars` support on
them is unnecessary for the Python backend — documenting this avoided new
contributor confusion.

---

### Next immediate steps

Phase-3 capture & verify targets are wired. Run:

```bash
make pact-capture   # regenerate contract JSON (fails if diff)
make pact-verify    # provider verification (skips if pact_verifier missing)
```

Next focus: Phase-4 property-based fuzzing via proptest.

---

### Nice-to-have / stretch

- [ ] Auto-publish interactive AsyncAPI docs to GitHub Pages
- [ ] Mock server for local-dev (`asyncapi mock`) – front-end can boot without backend
