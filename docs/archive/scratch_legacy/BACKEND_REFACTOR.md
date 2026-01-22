# Backend Refactor Task List: Configuration, Auth, and Database/CRUD

## Context

The codebase has undergone major improvements in registry design, tool aggregation, and testability. The next phase targets three high-leverage areas for maintainability, clarity, and operational robustness:

1. **Configuration Management**: Eliminate scattered `os.getenv` calls and centralize all configuration in a type-safe, dependency-injectable object.
2. **Auth Strategy Refactor**: Cleanly separate dev and prod authentication logic, making it easy to swap strategies and test.
3. **Database/CRUD Improvements**: Remove magic strings, improve type safety, and prepare for async DB access.

---

## 1. Centralized Configuration Management

**Goal:**
Replace all direct environment variable access (`os.getenv`, `os.environ[...]`) with a single, type-safe configuration object, injected via FastAPI dependencies.

**Why:**
- Eliminates config sprawl and runtime surprises.
- Makes it easy to override config in tests.
- Improves type safety and discoverability.

**Tasks:**
- [ ] Create a `Settings` class using `pydantic.BaseSettings` (e.g., `backend/zerg/config/settings.py`).
- [ ] Move all config flags (feature flags, secrets, URLs, etc.) into this class.
- [ ] Replace all `os.getenv`/`os.environ` usage in the codebase with `settings.<field>`.
- [ ] Inject `settings` via FastAPI dependency in all routers and services.
- [ ] Add a test fixture to override settings for tests.

---

## 2. Auth Strategy Refactor

**Goal:**
Cleanly separate dev and prod authentication logic using a strategy pattern, with zero runtime branching in the main dependency.

**Why:**
- Removes complex, error-prone branching in the request path.
- Makes it trivial to swap auth strategies for tests or different environments.
- Improves security and clarity.

**Tasks (completed – May 2025):**
- [x] Define an `AuthStrategy` interface (abstract base class).
- [x] Implement `DevAuthStrategy` (bypass, deterministic user) and `JWTAuthStrategy` (real JWT validation).
- [x] Select strategy at import-time based on `settings.auth_disabled` and expose via dependency.
- [x] Eliminated runtime branching from the main `get_current_user` dependency (now delegates to the chosen strategy).
- [x] Tests updated – legacy monkey-patches (`AUTH_DISABLED`, `JWT_SECRET`, etc.) remain compatible.

---

## 3. Database/CRUD Improvements

**Goal:**
Remove magic strings, improve type safety, and prepare for async DB access.

**Why:**
- Prevents bugs from typos in status/role/trigger fields.
- Makes the codebase more robust and self-documenting.
- Lays groundwork for async DB migration.

**Tasks (completed – May 2025):**
- [x] Introduced `UserRole`, `AgentStatus`, `RunStatus`, `RunTrigger`, and `ThreadType` enums under `zerg.models.enums`.
- [x] Migrated SQLAlchemy models to `Enum` columns (`native_enum=False` → CHECK constraints in SQLite).
- [x] CRUD helpers and routers continue to accept plain strings for backwards compatibility; equality checks work because enums inherit from `str`.
- [x] All tests updated (0 failures; 143 passed, 15 skipped).
- [ ] (Optional) Async migration deferred to a future milestone.

---

## Deliverables

- A single, type-safe, dependency-injectable `Settings` object.
- Auth logic that is strategy-based, testable, and branch-free at runtime.
- All status/role/trigger fields use enums, with no magic strings in the codebase.
- All changes covered by robust tests.

=============

Help me implement each of these tasks. Upon finishing one, please go back, review and update this task document.
