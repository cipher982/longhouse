# Account-Level Connector Credentials (v2)

## Status
- **Owner:** AI Assistant (Codex)
- **Last Updated:** 2025-11-30
- **Consumers:** Backend, Frontend, Infrastructure, Product
- **Links:** `apps/zerg/backend/zerg/models/models.py`, `apps/zerg/backend/zerg/routers/agent_connectors.py`, `apps/zerg/frontend-web/src/components/agent-settings/ConnectorCredentialsPanel.tsx`

---

## Assumptions & Goals
1. **No production users yet.** We can break compatibility as long as local/dev data migrates. The objective is to land the long-term architecture now instead of layering patches later.
2. **Two credential families exist today:**
   - `connectors` table (owner-level) powers Gmail/webhook triggers (`apps/zerg/backend/zerg/models/models.py:105`).
   - `connector_credentials` table (agent-level) powers built-in notification/project-management tools (`apps/zerg/backend/zerg/models/models.py:557`).
3. **Security + correctness first.** Secrets always flow through the existing Fernet utilities; no plaintext leaves the resolver boundary.
4. **Future org/workspace support matters.** Design must allow adding `organization_id` without tearing down schemas again.

---

## Current Architecture Snapshot
| Surface | Scope | Storage | APIs | Consumers |
|---------|-------|---------|------|-----------|
| **Legacy Connectors** | User-owned Gmail/webhook integrations | `connectors` table (`owner_id`, `type`, `provider`, `config`) | `/api/connectors` CRUD + trigger services | Gmail watch plumbing, webhook triggers |
| **Agent Connector Credentials** | Agent-specific built-in tool secrets | `connector_credentials` table | `/agents/{agent_id}/connectors/*` | Slack/GitHub/etc tools via `CredentialResolver`

**Pain Points**
- Users configure the same token for each agent; drawer UI is crowded.
- CredentialResolver only understands agent scope, so there is no abstraction for shared credentials.
- No automated tests protect the new API/router/resolver.
- Cache invalidation is undefined; long-lived workers never notice credential changes.

---

## Decisions (v2)
1. **Terminology split:**
   - *Provider Connectors* = existing `connectors` table for external providers that need bespoke OAuth/webhook flows (keep as-is for Gmail, etc.).
   - *Account Integrations* = new `account_connector_credentials` table for built-in Slack/GitHub/etc credentials used by tools at runtime. The two tables coexist cleanly.
2. **Ownership model:** credentials are user-owned today (`owner_id` FK) with an optional `organization_id` column (nullable) ready for multi-tenant rollout. Agents inherit from their owner; later we can flip agents to `organization_id` and migrate.
3. **Override semantics:** Agent-level overrides remain optional in `connector_credentials` so power users can isolate secrets. Resolution order: `agent_override → account_credential → None`.
4. **Resolver contract:** `CredentialResolver` is refactored to accept both `agent_id` and `owner_id`. Resolver instances cache per-request results keyed by connector type plus a monotonic `updated_at` version from each table so we can skip decrypts when nothing changed.
5. **Invalidation strategy:** add `updated_at` columns (already present) and store `(type, updated_at, scope)` in the resolver cache. When `updated_at` changes (detected by lightweight query), the resolver refreshes and clears per-type cache. For multi-worker deployments, we emit a `credential_updated` event via Redis pub/sub (stub now, easy to add later) but for MVP we rely on request-scoped resolvers (no long-lived cache) + webhook to notify websockets.
6. **Testing mandate:** add unit tests for resolver fallback logic, API contract tests for new `/account/connectors` router, and end-to-end tool tests that exercise resolver integration. Legacy `/api/connectors` tests remain untouched.
7. **Migration stance:** because no prod users exist, we ship the new storage + UI, migrate dev data automatically, and delete redundant per-agent entries once account-level configs exist. Provide a reversible script for safety.

---

## Data Model
### New Table: `account_connector_credentials`
```sql
CREATE TABLE account_connector_credentials (
    id SERIAL PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id INTEGER NULL REFERENCES organizations(id) ON DELETE CASCADE,
    connector_type VARCHAR(50) NOT NULL,
    encrypted_value TEXT NOT NULL,
    display_name VARCHAR(255),
    test_status VARCHAR(20) NOT NULL DEFAULT 'untested',
    last_tested_at TIMESTAMP,
    connector_metadata JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(owner_id, connector_type)
);
```
> `organization_id` remains NULL until teams ship. When populated, we enforce `(organization_id, connector_type)` uniqueness and agents reference `organization_id` to resolve credentials.

### Relationship Summary
```
User (owner_id)
 ├─ Provider Connectors (type/provider)
 ├─ Account Integrations (connector_type)
 │    └─ optional Agent overrides (connector_credentials)
 └─ Agents
        └─ CredentialResolver(agent_id, owner_id)
```

---

## Backend Plan
### Phase 0 – Prep (0.5 day)
- **Add owner FK to resolver path:** extend `CredentialResolver.__init__` to accept `owner_id: int`. Update AgentRunner (`apps/zerg/backend/zerg/managers/agent_runner.py`) to pass both the agent row and owner id.
- **Add light observability:** structured logs when resolver falls back to account-level credentials to make debugging trivial.

### Phase 1 – Data Layer (1 day)
1. **Alembic migration:** create table + indexes above and add SQLAlchemy model `AccountConnectorCredential` in `apps/zerg/backend/zerg/models/models.py`.
2. **CRUD helpers:** add `account_connector_credentials` CRUD module + service wrappers. Ensure encryption uses existing `encrypt/decrypt` utilities.
3. **Resolver logic:**
   - `get()` now resolves in this order: `_get_agent_override(type)`, `_get_account_credential(type, owner_id)`, else `None`.
   - Cache stores `{type: (source, payload, version)}` so repeated calls within a request stay in-memory.
   - `has()` first checks cache, otherwise queries minimal columns (`updated_at`) to avoid decrypting.
4. **Context helpers:** allow `set_credential_resolver` to receive `owner_id` (passed through or encapsulated in resolver instance).

### Phase 2 – Account Connectors API (1.5 days)
- New router `apps/zerg/backend/zerg/routers/account_connectors.py` with parity endpoints:
  - `GET /account/connectors`
  - `POST /account/connectors`
  - `POST /account/connectors/test`
  - `POST /account/connectors/{type}/test`
  - `DELETE /account/connectors/{type}`
- Requests use same schemas as agent endpoints but without `agent_id`. Response includes metadata + test status.
- Auth: reuse `get_current_user`; no org support yet.
- Deprecate agent endpoints in UI (but keep for overrides – hide behind “Advanced” section computing off new router).

### Phase 3 – Frontend (2 days)
1. **Settings shell:** add `/settings/integrations` route + page component under `apps/zerg/frontend-web/src/pages/settings`.
2. **Hooks:** introduce `useAccountConnectors` etc mirroring the agent hooks but without agent scoping. Keep agent hooks for overrides until they’re removed from the drawer.
3. **Agent drawer UX:**
   - Replace `ConnectorCredentialsPanel` content with: “Integrations are managed in Settings → Integrations” plus a collapsed “Advanced Overrides” accordion that reuses the old panel for the small subset of users who need per-agent secrets.
   - Badges in the Allowed Tools section show “Configured at account level” or “Missing integration, click to configure”.

### Phase 4 – Migration & Cleanup (1 day)
- Script: `scripts/migrate_agent_credentials.py`.
  1. Group `connector_credentials` by `(owner_id, connector_type)`.
  2. If all encrypted blobs identical → create account credential and delete duplicates.
  3. If multiple distinct blobs exist → promote the most recently updated as default, keep the rest as agent overrides.
  4. Emit summary for manual review.
- Because there are no prod users, run script automatically on deploy; keep backup table `connector_credentials_legacy` for rollback.
- Remove legacy UI references once migration verified.

---

## Testing & Verification
| Layer | Tests |
|-------|-------|
| **Unit** | - Resolver fallback path (agent override, account fallback, cache invalidation).<br>- Encryption helpers for new model (round-trip). |
| **API** | - `/account/connectors` CRUD happy paths + auth guards.<br>- Negative tests for missing required fields, double configuration, deletion of non-existent connectors. |
| **Integration** | - Tool smoke tests (e.g., Slack webhook) using resolver context to ensure inherited creds work.<br>- Migration script dry-run test with fixtures representing conflicting credentials. |
| **Frontend** | - React Testing Library coverage for Integrations page (list, configure, delete) and Agent drawer badges. |

Add GitHub workflow step running new backend tests plus Cypress (or Playwright) regression when available.

---

## Observability & Ops
- Structured log events `credential_resolver.resolve` with fields `{agent_id, owner_id, connector_type, source: 'agent'|'account', cache_hit: bool}`.
- Emit `metrics.connector_credentials.total` gauges for account-level integrations to monitor adoption.
- Future: push pub/sub invalidation events once multi-worker load warrants it.

---

## Risks & Mitigations
1. **Resolver signature churn** – update all call sites (AgentRunner, tests, potential scripts). Mitigation: type-check + grep for `CredentialResolver(`.
2. **Migration mistakes** – even without prod users, dev/test data matters. Mitigation: create backup tables and offer `--dry-run` mode printing actions.
3. **UI confusion** – users may not find new settings page. Mitigation: add banner in Agent drawer linking directly to Settings, plus onboarding tooltip.
4. **Future org support** – storing `organization_id` now avoids another migration; document how to backfill when orgs ship.

---

## Next Steps
1. Land Alembic migration + ORM model.
2. Update resolver + AgentRunner, add tests.
3. Build `/account/connectors` API + UI page.
4. Wire migration script and remove old drawer UI.
5. Plan org-level follow-up once teams feature is scheduled.

