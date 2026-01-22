# Zerg Platform – Google-only Auth MVP  🚧 Implementation Road-map

> **Goal:** Ship a *minimal but production-grade* authentication layer based on
> Google Sign-In (OIDC), plus a `AUTH_DISABLED` dev bypass so local hacking
> remains friction-free.  This document is a living checklist – tick boxes as
> tasks land.

-------------------------------------------------------------------------------
Legend
-------------------------------------------------------------------------------
* `[ ]` = not started   `[~]` = in progress   `[x]` = merged to `main`
* File & class hints use **monospace**.  Keep patches small and target files
  directly to simplify code-review.

-------------------------------------------------------------------------------
Stage 0 – Pre-work (scaffolding)
-------------------------------------------------------------------------------
| # | Task | Pointers | Status |
|---|------|----------|--------|
| 0.1 | Decide & store Google *Client ID* / allowed redirect URIs          | Google Cloud Console | [x] |
| 0.2 | Add secrets to `.env.example` (`GOOGLE_CLIENT_ID`, `JWT_SECRET`, `AUTH_DISABLED`) | **repo-root/.env.example** | [x] |

-------------------------------------------------------------------------------
Stage 1 – Database model & migration
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 1.1 | Create **User** table (`id, email, provider, provider_user_id, is_active, timestamps`) | `backend/zerg/models/models.py` | [x] |
| 1.2 | Lightweight Alembic migration or `Base.metadata.create_all()` fallback | `backend/zerg/database.py` | [x] |
| 1.3 | CRUD helpers `get_user_by_email`, `create_user`, `get_user`            | `backend/zerg/crud/crud.py` | [x] |

-------------------------------------------------------------------------------
Stage 2 – Auth routes & token minting
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 2.1 | New router **`auth.py`** with prefix `/api/auth`                     | `backend/zerg/routers/auth.py` | [x] |
| 2.2 | Endpoint **POST `/api/auth/google`** (`{ "id_token": str }`)         | same | [x] |
| 2.3 | Validate Google ID-token (`python-jose` + cached JWKS)                | same | [x] |
| 2.4 | Upsert user → issue short-lived HS256 JWT (`access_token`)            | same | [x] |
| 2.5 | Pydantic schema **`TokenOut`**                                        | `backend/zerg/schemas/schemas.py` | [x] |

-------------------------------------------------------------------------------
Stage 3 – Auth dependency & dev-mode bypass
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 3.1 | Utility `get_current_user()` (`dependencies/auth.py` new file)         | `backend/zerg/dependencies/auth.py` | [x] |
| 3.2 | Implement branch: if `AUTH_DISABLED==1` → always return *dev user*     | same | [x] |
| 3.3 | If enabled: verify JWT, load user, raise 401 if missing/expired        | same | [x] |
| 3.4 | Inject `current_user: User = Depends(get_current_user)` into **ALL** existing routers (except `/models`) | each router | [x] |

> **Update (2025-05-02):** All Stage 3 items have landed.  The backend now
> enforces JWT auth across every router except the public `/models` and
> `/auth` endpoints.  Setting `AUTH_DISABLED=1` in the environment bypasses
> auth and automatically returns/creates a deterministic *dev@local* user –
> this keeps the existing test-suite and local hacking workflow functional.

-------------------------------------------------------------------------------
Stage 4 – Front-end integration
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 4.1 | Add Google Identity script tag ⇒ `www/index.html`                     | `frontend/www/index.html` | [x] |
| 4.2 | Rust/JS glue: render Google button, forward `credential` to API       | `frontend/src/components/auth.rs` (new) | [x] |
| 4.3 | Extend **`ApiClient`** → `google_auth_login(id_token)`                | `frontend/src/network/api_client.rs` | [x] |
| 4.4 | Persist JWT (`localStorage["zerg_jwt"]`)                              | same | [x] |
| 4.5 | Attach `Authorization: Bearer …` in `fetch_json()` if present         | same | [x] |
| 4.6 | WebSocket: append `?token=<jwt>` on connect                           | `frontend/src/network/ws_client_v2.rs` | [x] |
| 4.7 | Simple *logged-in* flag in `AppState` to show / hide login modal      | `frontend/src/state.rs` | [x] |
| 4.8 | Gate initial data fetch & WS connect until user is authenticated      | `frontend/src/lib.rs`, `frontend/src/components/auth.rs` | [x] |
| 4.9 | CSS for `.login-overlay` (full-screen, centre button, `.hidden`)      | `frontend/www/styles.css` | [x] |
| 4.10 | Show loading / error feedback during login flow                      | `frontend/src/components/auth.rs` | [x] |
| 4.11 | Logout flow (clear token, show overlay, close WS)                    | frontend | [x] |
| 4.12 | JWT expiry / 401 handling → automatic re-login (refresh TBD)         | frontend / backend | [x] |
| 4.13 | Replace `eval()` glue with safe JS interop (CSP-friendly)             | `frontend/src/components/auth.rs` | [x] |
| 4.14 | Provide `GOOGLE_CLIENT_ID` to the frontend                           | runtime fetch via `/api/system/info` | [x] |
|      | (final impl switched from compile-time `env!` to runtime flags)      |               |     |

-------------------------------------------------------------------------------
Stage 5 – Trigger hardening (HMAC header)
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 5.1 | Spec headers `X-Zerg-Timestamp`, `X-Zerg-Signature`                   | `backend/zerg/routers/triggers.py` | [x] |
| 5.2 | Implement 5-min replay-window & HMAC-SHA256 validation                | same | [x] |
| 5.3 | Update tests (see `tests/test_triggers.py`)                           | `backend/tests/…` | [x] |

> **Update (2025-05-07):** Stages **4** (Frontend integration) and **5**
> (Trigger hardening) have landed on `main`.  Google Sign-In is now fully
> wired from browser → backend, JWTs are attached to REST & WebSocket
> traffic, and webhook triggers are protected via HMAC-SHA256 with a
> ±5-minute replay window.
>

-------------------------------------------------------------------------------
Final status (2025-05-18)
-------------------------------------------------------------------------------

All stages (0-9) have been fully implemented and merged to `main`:

• Unit & integration tests are green – 135 total tests cover Google sign-in,
  JWT handling, WebSocket auth and the dev-mode bypass.
• Front-end stores the JWT, attaches it to every REST call **and** adds it as
  the `?token=` query-param for WebSocket upgrades.
• Back-end validates the token prior to the WS handshake (close-code **4401** on
  failure) and propagates the resolved `user_id` to `TopicConnectionManager`.
• Docs, README quick-start and pre-commit hooks are up-to-date.

No open items remain – the Google-only authentication MVP is **complete** and
ready for production roll-out.  Future work (refresh-tokens, SSO expansion)
will be tracked in a new roadmap.

-------------------------------------------------------------------------------
Stage 6 – Tests & quality gates
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 6.1 | Unit-tests for ID-token verification & JWT minting                    | `backend/tests/test_auth_google.py` | [x] |
| 6.2 | Integration test: unauthenticated request returns 401 (prod-mode)     | same | [x] |
| 6.3 | Integration test: `AUTH_DISABLED=1` bypass works                      | same | [x] |
| 6.4 | Front-end wasm-bindgen test: auth utils & logout helpers             | `frontend/src/utils.rs` | [x] |

-------------------------------------------------------------------------------
Stage 7 – Docs & housekeeping
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 7.1 | Update **README.md** quick-start (mention Google sign-in & bypass)    | `README.md` | [x] |
| 7.2 | Add security note to `docs/auth_overview.md` → “Phase A shipped”      | `docs/auth_overview.md` | [x] |
| 7.3 | Verify `pre-commit` passes, fix new Ruff warnings, bump `ruff.toml` exclude list if needed | repo root | [x] |

-------------------------------------------------------------------------------
Stage 8 – WebSocket auth hardening *(post-MVP)*
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|----|------------------------------------------------------------------------------------------------|---------------------------------------------|--------|
| 8.1 | Extract `validate_ws_jwt()` helper (reuse logic from `get_current_user`)                      | `zerg/dependencies/auth.py`                 | [x] |
| 8.2 | In `routers/websocket.py` verify `token` *before* `accept()`; on failure close **4401**       | `backend/zerg/routers/websocket.py`         | [x] |
| 8.3 | Front-end always appends `?token=<jwt>`                                                      | `frontend/src/network/ws_client_v2.rs`      | [x] |
| 8.4 | Tests: (i) blocked w/o token (ii) succeeds w/ token (iii) bypass mode                         | `backend/tests/test_websocket_auth.py`      | [x] |
| 8.5 | Propagate resolved `user_id` to `TopicConnectionManager` (prep per-user topics)               | `backend/zerg/websocket/manager.py`         | [x] |
| 8.6 | Docs: mention WebSocket close-code **4401 Unauthorized**                                      | `docs/auth_overview.md`                     | [x] |

-------------------------------------------------------------------------------
Appendix – Dev user helper
-------------------------------------------------------------------------------
*Helper function to auto-create a deterministic user when `AUTH_DISABLED` is on.*

```python
# backend/zerg/dependencies/auth.py
DEV_EMAIL = "dev@local"

def _get_or_create_dev_user(db: Session) -> User:
    from zerg.crud import crud
    if (u := crud.get_user_by_email(db, DEV_EMAIL)):
        return u
    return crud.create_user(db, email=DEV_EMAIL, provider=None)
```

FYI: The upcoming `validate_ws_jwt()` helper (see 8.1) will call the same
logic, so WebSocket connections inherit the **AUTH_DISABLED** development
bypass automatically.

-------------------------------------------------------------------------------
This roadmap lives in **`auth_implementation_roadmap.md`** – keep it updated in
every PR so the whole team sees progress at a glance.  Happy coding! 🛠️

-------------------------------------------------------------------------------
Stage 9 – Per-user topics & permissions *(stretch)*
-------------------------------------------------------------------------------
| # | Task | Code Location | Status |
|---|------|---------------|--------|
| 9.1 | TopicManager supports `user:{id}` topic for profile / settings updates | `backend/zerg/websocket/manager.py` | [x] |
| 9.2 | Front-end subscribes to current-user topic (refresh user state on change) | `frontend/src/update.rs` | [x] |
| 9.3 | Permission groundwork: add `role` column (USER / ADMIN) to User model  | `backend/zerg/models/models.py` | [x] |
