# User Personalisation & Identity UI – Implementation Plan

> **Audience:** A full-stack engineer who is **new to this code-base** and has to add “user identity” polish after Google Sign-In was introduced.  This document is intentionally self-contained – you should be able to start coding without reading any other internal docs first.

---

## 1  Why do we need this?

Authentication landed: users can log in with Google and we issue our own JWT.  **Nothing in the UI changes afterwards**, so the product still feels anonymous and multi-tenant.  Our goals:

1. Visually confirm to the user that they are signed in (avatar, name, online status).
2. Give access to a *Profile* page where users can edit basic preferences (display name, avatar, theme …).
3. Make data feel *personal* (filter lists to “My agents” by default, etc.).


## 2  TL;DR for the busy dev

• Add `display_name`, `avatar_url`, timestamps to the **User** model.
• New endpoints: `GET /api/users/me`, `PUT /api/users/me`.
• Front-end: load `CurrentUser` into global `AppState`, show circle avatar top-right, dropdown (`Profile • Settings • Logout`).
• Build `/profile` page: read/write prefs.
• Sprinkle personal touches (greeting, “My agents” filter, avatar beside chat messages).
Estimated **3–4 dev-days** end-to-end.


## 3  Code-base Crash-Course (60 seconds)

| Area | Tech | Entry point | Quick mental model |
|------|------|------------|----------------------|
| Backend | Python 3.12 + FastAPI | `backend/zerg/main.py` | REST & WS, event bus pattern, SQLAlchemy models |
| Frontend | Rust → WebAssembly | `frontend/src/lib.rs` | Elm-style Msg/Update, global `AppState`, topic-based WebSocket |
| Auth (new) | Google ID-token → JWT | `backend/zerg/routers/auth.py` • `frontend/src/utils::current_jwt` | Already exchanges Google token and stores JWT in localStorage |


## 4  Proposed UX Additions

### 4.1 Global Top-Bar

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Agent Platform                                    • • •  🟢 A          │
└─────────────────────────────────────────────────────────────────────────┘
```

• `🟢` dot = websocket connected.
• `A` = user avatar (first letter fallback).
• Click → dropdown: **Profile • Settings • Sign out**.

### 4.2 Profile Page (`/#/profile`)

| Avatar upload | Display name | Email (read-only) | Theme switch | Time zone |

`Save` button triggers `PUT /users/me`.

### 4.3 Dashboard tweaks
* Default filter: **“My agents”** (owned_by == current_user.id).
* Agents list shows an **Owner** column.
* Header text “Welcome back, {first_name}!”.

### 4.4 Chat view
* Own messages are prefixed with small avatar & display_name.


## 5  Backend Tasks – Status

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Extend `User` model with display_name, avatar_url, prefs, last_login | **DONE** (`models/models.py`) |
| 2 | Add `crud.update_user()` helper | **DONE** |
| 3 | Add/extend schemas `UserOut`, **new** `UserUpdate` | **DONE** |
| 4 | Create **users router** <br/>• `GET /api/users/me` <br/>• `PUT /api/users/me` | **DONE** (`routers/users.py`) |
| 5 | Emit `USER_UPDATED` over EventBus & WebSocket (`user:{id}` topic) | **DONE** – Topic manager hooked up |
| 6 | Include name / avatar in issued JWT | **BACKLOG / optional** |

### New regression tests

`backend/tests/test_users.py` covers:

• Fetching the current profile (GET) returns valid structure.
• Updating profile (PUT) persists and reflects on subsequent GET.
• `USER_UPDATED` EventBus message is emitted (captured with `AsyncMock`).

All backend tests now: **102 passed, 15 skipped**.


## 6  Frontend Tasks

### 6.1 Domain model  ✅ *implemented*
Rust structs are now shipped in `frontend/src/models.rs`:
```
pub struct CurrentUser { … }

// AppState (frontend/src/state.rs)
pub current_user: Option<CurrentUser>,
pub logged_in: bool,
```

They deserialize directly from `/api/users/me`.

### 6.2 Load flow  ✅ *complete*
1 After `google_auth_login()` the Rust side now:
   • stores JWT -> localStorage (existing)
   • calls **new** `ApiClient::fetch_current_user()`
   • dispatches **Message::CurrentUserLoaded**.
2 `update.rs` stores `state.current_user` & sets `logged_in=true`.
3 Subscribes to topic `user:{id}` once profile is known; WS `user_update` frames now refresh avatar live.

New helper fns in `ApiClient`:
```rust
async fn fetch_current_user() -> Result<String, JsValue>
async fn update_current_user(patch_json: &str) -> Result<String, JsValue>
```

### 6.3 Components  ✅
* **AvatarBadge** – implemented in `frontend/src/components/avatar_badge.rs`.
* **UserMenu** – implemented & mounted automatically – shows Avatar, dropdown (Profile / Logout) and updates on WS events.
* **ProfilePage** – two-column form; on *Save* the page issues a direct `PUT /users/me` call and then dispatches `Message::CurrentUserLoaded` with the updated profile so all UI surfaces refresh.

### 6.4 Routing  ✅ *complete*
`window.onhashchange` listener implemented.  Navigating to `#/profile`,
`#/dashboard`, `#/canvas` and `#/chat` now dispatches the correct
`ToggleView` message on page load and during hash changes.

### 6.5 Sprinkle personalisation
* **DONE** – Header greeting replaces static text with *Welcome, {display_name ∥ email}*.
* **DONE** – Dashboard table defaults to **My agents**; when *All agents* is selected an **Owner** column is rendered (live).
* **DONE** – Chat bubbles now show miniature avatar + display_name for user messages.


## 7  Non-Code Work

| Task | Owner | Notes |
|------|-------|-------|
| Avatar placeholder design | Design | Use initials + random bg colour (Material 3 spec) |
| Copywriting | PM | Revise empty-state texts: “Sign in to create your first agent” |
| Accessibility review | QA | Ensure avatar menu usable with keyboard + aria-labels |


## 8  Stretch Ideas
* Token expiry toast with “Refresh session” button.
* Usage meter (“18K tokens this month”).
* Organisation / team switcher in avatar dropdown.
* Dark / light mode preference persisted in `prefs` JSON.


## 9  Suggested Implementation Sequence & Time-line

| Day | Deliverable |
|-----|-------------|
| 0   | **DONE** – DB columns, CRUD helper, schemas, routes, tests |
| 1   | **DONE** – Frontend plumbing: CurrentUser in state, fetch after login |
| 1½  | **DONE** – WS `user:{id}` subscription & live updates |
| 2   | **DONE** – AvatarBadge & UserMenu in header (incl. WS status) |
| 2½  | **DONE** – Profile page UI (basic), WS live update wiring |
| 3   | **DONE** – hash-router, header greeting, chat avatars |
| 4   | **DONE** – ownership column, `/api/agents?scope` filter, Dashboard UI toggle & Owner column |
| 4½  | **DONE** – Persist dashboard scope in `AppState` / localStorage |
| 5   | **DONE** – Playwright cross-browser test (scope persistence), docs/screenshots |

---

# 2025-05-21 Frontend Dashboard Scope – Progress Note (evening)

✅ **Dashboard scope selector implemented** (Rust/WASM):

• New `DashboardScope` enum + `dashboard_scope` field in `AppState` (persisted in `localStorage`).
• `<select>` dropdown added to dashboard header – toggles *My agents* ⇄ *All agents*.
• Message `ToggleDashboardScope` wired through `update.rs` reducer – triggers `FetchAgents` command.
• `ApiClient::get_agents_scoped(?scope=…)` helper + executor update.
• Table header & rows dynamically add **Owner** column when `scope = all`.  Uses existing `AvatarBadge` component for a small circle + owner label.
• Empty-state colspan adjusts automatically.
• Scope preference is remembered across sessions.

Additional work:
• `ApiAgent` struct now carries `owner_id` + nested `owner`; unit-test added (`test_api_agent_with_owner`).
• Dashboard row struct extended, search/col-builder refactor.

Outstanding bits:
• Re-style header flex-layout (scope selector is currently left-aligned).

Backend untouched – existing `/api/agents?scope` endpoint already covers the UI.

✅ Backend ownership groundwork merged (PR #321):

• Added `owner_id` FK to **Agent** model & relationship.
• `crud.create_agent()` requires `owner_id`; `get_agents()` supports optional filter.
• `GET /api/agents?scope=my|all` implemented – admin-guard checked.
• Schemas extended (`owner_id`, nested `owner`).
• All 135 backend tests pass.

Next up – **frontend Dashboard** work (scope toggle, column render, localStorage persistence).


## 10  Testing Matrix

| Area | Test | Tool |
|------|------|------|
| API  | unauth → 401,  auth → 200 | pytest |
| Front | login → avatar visible | wasm-bindgen-tests / Playwright |
| WS    | update display_name in tab A → dropdown in tab B refreshes | Playwright (multi-page) |


## 11  Definition of “Done”

1. User sees their avatar & name after signing in.
2. Profile page persists changes immediately and broadcasts via WS.
3. Dashboard defaults to *My agents* and shows owner column.  **DONE**
4. Wasm-bindgen test for `CurrentUser` deserialization.  **DONE**
5. Unit + integration tests green on backend.
6. No regressions in existing CI suites.


---

### Appendix A – Useful file paths

```
backend/zerg/models/models.py        ← SQLAlchemy models (add columns here)
backend/zerg/routers/                ← add users.py router
backend/zerg/dependencies/auth.py    ← current_user dep (update last_login)
frontend/src/state.rs                ← add current_user field
frontend/src/models.rs               ← define CurrentUser struct
frontend/src/components/             ← new AvatarBadge, UserMenu, ProfilePage
```

Good luck — and ping @product if anything is unclear!  🚀


---

## 12  Outstanding Work (May 2025 audit)

The following tasks remain before we can close the feature flag:

1. Add `display_name` & `avatar_url` to issued JWT  **DONE** (backend `auth._issue_access_token`).
2. Playwright E2E tests: assert Owner column visibility + scope persistence across reload.  **DONE** ✅  (see `prerender/tests/` specs)

With E2E coverage in place we can now remove the *user-personalisation* feature flag once UI polish (header alignment) is addressed.
