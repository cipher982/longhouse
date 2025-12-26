# ⚠️ ARCHIVED / HISTORICAL REFERENCE ONLY

> **Note:** Paths and implementation details in this document may be outdated.
> For current information, refer to [AGENTS.md](../../AGENTS.md) or the root `docs/README.md`.

---

> ⚠️ **HISTORICAL (Dec 2024):** This document predates the unified SPA merge (2025-12-20).
> Jarvis no longer has a standalone `apps/zerg/frontend-web/src/jarvis/` frontend; the chat UI lives in `apps/zerg/frontend-web/src/jarvis/` and is served at `/chat`.

⏺ Zerg Platform Refactor - Phase 2: Shared Packages (Ready to Start)

## Project Background

Zerg is an AI agent orchestration platform (FastAPI + Python + LangGraph). Jarvis is a voice/text UI (React PWA + Express server) that connects to Zerg. Together they form a "swarm" system in a Bun/uv monorepo.

**Tech Stack:**

- Backend: FastAPI, SQLAlchemy, LangGraph, PostgreSQL (Python via uv)
- Frontend: React 18, Vite, React Query (TypeScript via Bun)
- Jarvis: React 19 PWA + Express server (Bun workspace)
- Deployment: Docker Compose, designed for Coolify

**Repository:** `/Users/davidrose/git/zerg/`

---

## What's Been Done

### Phase 1: Docker Consolidation (Complete ✅)

- Commit: `98269e0` - "refactor: Phase 1 - unified Docker Compose with profiles"
- Unified 3 separate compose files into one with profiles (zerg, full, prod)
- Consolidated environment variables into single `.env.example`
- Added `make env-check` and `make env-check-prod` validation

### Jarvis React Migration (Complete ✅)

**Core Migration (6 commits)**

- Commits: `b0f851e` through `c7427be`
- Migrated from vanilla TypeScript to React 19
- Created React components with TypeScript
- Implemented Context + useReducer for state management
- Built custom hooks (useVoice, useTextChannel, useJarvisClient, useRealtimeSession)
- Added PWA service worker with offline support
- Feature flag: `VITE_JARVIS_ENABLE_REALTIME_BRIDGE` (default: false)

**React Migration Hardening (10 commits - Dec 9, 2024)**

- Commits: `82bedf9` through `e0d4021`
- **Fixed bridge mode issues:** Auto-connect, PTT initialization, backend integration
- **Added error handling:** Text send failures, connection errors, optimistic rollback
- **Implemented retry/reconnect:** Connection state management, reconnect action
- **Enhanced UI feedback:** Connection states (idle/connecting/ready/error), disabled controls
- **Comprehensive testing:** Fixed 9 failing test suites, added PWA tests, added e2e suite
- **Documentation:** Updated migration docs, added E2E testing guide

---

## Current State (Dec 9, 2024)

### Git Status

```bash
Current branch: main
Latest commit: e0d4021 - "test(jarvis): add bridge mode e2e smoke tests"
Clean working directory
```

**Recent commits (last 11):**

```
e0d4021 test(jarvis): add bridge mode e2e smoke tests
92e5b7f feat(jarvis): disable PTT when not connected, add connection states
ecce152 fix(jarvis): add retry/reconnect for realtime init failures
fd471d1 fix(jarvis): add error handling and rollback for text sends
2db5f73 fix(jarvis): resolve models.json path in Vitest
e5b1011 test(jarvis): add comprehensive PWA offline tests
f85b926 docs(jarvis): update migration docs and remove confusing deadline
83a6398 fix(jarvis): wire useTextChannel to backend in bridge mode
82bedf9 fix(jarvis): bridge mode auto-connect and PTT button initialization
4031c1a test: add React integration tests for core flows
a05c8ff chore: quarantine legacy code with deprecation notices
```

### Docker Status

All services healthy:

```
postgres           - Up (healthy)
backend       - Up (healthy)
frontend-web      - Up (healthy)
jarvis-web         - Up (healthy)
reverse-proxy      - Up (healthy)
```

**Working Endpoints:**

- `http://localhost:30080/` → Zerg dashboard (React)
- `http://localhost:30080/dashboard` → Zerg dashboard (alias)
- `http://localhost:30080/chat/` → Jarvis PWA (React)

### Test Status ✅

```
Test Files: 16 passed | 1 skipped (17)
Tests: 174 passed | 5 skipped (179)
```

**Test suites:**

- Unit tests: 174 passing (was 165 before hardening)
- PWA offline tests: 8 tests
- React integration tests: 6 tests
- E2E tests: 5 tests (skipped by default, run with `VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true`)

**What was fixed:**

- ✅ All 9 previously failing test suites now pass
- ✅ Fixed models.json path resolution in Vitest
- ✅ Added comprehensive test coverage for new features

---

## Your Mission: Phase 2 - Shared Packages

**Status:** Ready to start (infrastructure solid, tests passing, React migration hardened)

### Goal

Create shared packages for code reuse between Zerg frontend and Jarvis web. Both are now React apps - time to DRY up common code and share configuration.

### What Needs to be Created

#### 1. `packages/config` - Shared Configuration (REQUIRED)

**Problem:**

- `config/models.json` is imported via brittle relative paths
- Path breaks in test environments (required Vitest alias workaround)
- Both Python (Zerg backend) and TypeScript (frontends) need access
- No typed exports for TypeScript consumers

**Solution:**
Create proper workspace package with:

- Typed TypeScript exports from `models.json`
- Single source of truth for model configuration
- Clean imports: `import { REALTIME_TIER_1 } from '@swarm/config'`
- Works in all environments (dev, test, Docker)

**Current imports to replace:**

```typescript
// apps/zerg/frontend-web/src/jarvis/core/model-config.ts
import modelsConfig from "../../../config/models.json"; // BRITTLE

// Should become:
import { modelsConfig } from "@swarm/config";
```

#### 2. `packages/utils` - Shared TypeScript Utilities (INVESTIGATE)

**Question:** Is there actually duplicated code between frontends?

**What to look for:**

- Date/time formatters
- API client utilities
- Type guards and validators
- Common React hooks (useDebounce, useLocalStorage, etc.)
- Shared types (Message, User, etc.)

**If found:**
Create package with:

- Shared utility functions
- Common TypeScript types
- Reusable custom hooks
- Type-safe helpers

**If NOT found:**
Skip this package. Don't create abstractions that don't exist yet.

#### 3. `packages/ui` - Shared React Components (OPTIONAL)

**Question:** Are there truly reusable components?

**What to look for:**

- Button styles used in both apps
- Loading states / spinners
- Error boundaries
- Layout components
- Typography components
- Form inputs

**Criteria for inclusion:**

- Component is used (or would be used) in BOTH apps
- Component is truly generic (not domain-specific)
- Component saves significant duplication

**If found:**
Create package with shared design system.

**If NOT found:**
Skip this package. Premature abstraction is worse than duplication.

---

## Current Structure

```
/Users/davidrose/git/zerg/
├── apps/
│   ├── zerg/
│   │   ├── backend/         # FastAPI (Python via uv)
│   │   ├── frontend-web/    # React 18 dashboard
│   │   └── e2e/            # Playwright tests
│   └── jarvis/
│       ├── apps/
│       │   ├── web/        # React 19 PWA (HARDENED ✅)
│       │   └── server/     # Express bridge
│       └── packages/       # Jarvis-specific packages
│           ├── core/       # Logger, session manager, model config
│           └── data/local/ # IndexedDB storage
├── config/
│   └── models.json        # ⚠️ NEEDS TO BE PACKAGED
├── schemas/               # API contracts (OpenAPI, AsyncAPI)
├── docker/
│   └── docker-compose.yml # Unified compose (Phase 1)
├── package.json           # Root workspace config
└── packages/              # ⚠️ TARGET LOCATION (TO BE CREATED)
    ├── config/           # Phase 2: Create this
    ├── utils/            # Phase 2: Maybe create this
    └── ui/               # Phase 2: Maybe create this
```

**Root workspace (`package.json`):**

```json
"workspaces": [
  "apps/zerg/frontend-web/src/jarvis/apps/*",
  "apps/zerg/frontend-web/src/jarvis/packages/*",
  "apps/zerg/frontend-web/src/jarvis/packages/data/*",
  "apps/zerg/frontend-web",
  "apps/zerg/e2e"
  // Add: "packages/*" after creating Phase 2 packages
]
```

---

## Phase 2 Implementation Steps

### Step 1: Investigate What to Share (Research)

#### Analyze Zerg Frontend

```bash
cd apps/zerg/frontend-web/src

# Check for utility directories
ls -la lib/ utils/ hooks/ 2>/dev/null

# Search for exportable utilities
grep -r "export.*function" --include="*.ts" --include="*.tsx" src/ | head -30

# Look for shared types
grep -r "export.*type\|export.*interface" --include="*.ts" --include="*.tsx" src/ | head -30

# Check for constants
grep -r "export const" --include="*.ts" src/ | head -30
```

#### Analyze Jarvis Frontend

```bash
cd apps/zerg/frontend-web/src/jarvis/src

# Check for utility directories
ls -la hooks/ components/ lib/

# Search for potentially reusable hooks
ls hooks/

# Look for reusable components
ls components/

# Check for utilities
grep -r "export.*function" --include="*.ts" --include="*.tsx" src/ | head -30
```

#### Compare for Duplication

Look for:

1. **Identical utility functions** - Same name, same signature
2. **Similar patterns** - Different names but same logic
3. **Shared types** - `Message`, `User`, `Session`, etc.
4. **Common hooks** - Debounce, local storage, media queries
5. **Reusable components** - Buttons, inputs, modals

**Document findings:**

```bash
# Create investigation notes
cat > PHASE_2_FINDINGS.md << 'EOF'
# Phase 2: Shared Package Investigation

## Duplicated Code Found

### Utilities
- [ ] Date formatters: zerg uses X, jarvis uses Y
- [ ] API helpers: both have fetch wrappers
- [ ] ...

### Types
- [ ] Message type: defined in both apps
- [ ] ...

### Components
- [ ] Button: similar but not identical
- [ ] ...

## Conclusion

- packages/config: REQUIRED (models.json)
- packages/utils: YES/NO because...
- packages/ui: YES/NO because...
EOF
```

### Step 2: Create `packages/config` (Required)

````bash
# Create directory structure
mkdir -p packages/config/src

# Create package.json
cat > packages/config/package.json << 'EOF'
{
  "name": "@swarm/config",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "src/index.ts",
  "types": "src/index.ts",
  "exports": {
    ".": "./src/index.ts"
  }
}
EOF

# Create TypeScript exports
cat > packages/config/src/index.ts << 'EOF'
/**
 * @swarm/config - Shared configuration
 * Single source of truth for model configuration
 */

// Import the JSON file (works in Vite and Bun)
// @ts-expect-error - JSON imports work with bundlers
import modelsConfigRaw from '../../../config/models.json'

// Export the raw config
export const modelsConfig = modelsConfigRaw

// Export typed model tiers for easy access
export const REALTIME_TIER_1 = modelsConfig.realtime.tiers.TIER_1
export const REALTIME_TIER_2 = modelsConfig.realtime.tiers.TIER_2
export const TEXT_TIER_1 = modelsConfig.text.tiers.TIER_1
export const TEXT_TIER_2 = modelsConfig.text.tiers.TIER_2
export const TEXT_TIER_3 = modelsConfig.text.tiers.TIER_3

// Export types
export type ModelTier = 'TIER_1' | 'TIER_2' | 'TIER_3'
export type ModelProvider = 'openai'

export interface ModelInfo {
  displayName: string
  provider: ModelProvider
  tier: ModelTier | null
  description: string
}

export interface ModelsConfig {
  text: {
    tiers: Record<ModelTier, string>
    mock: string
    models: Record<string, ModelInfo>
  }
  realtime: {
    tiers: {
      TIER_1: string
      TIER_2: string
    }
    models: Record<string, ModelInfo>
    aliases: Record<string, string>
    defaultVoice: string
  }
}
EOF

# Create README
cat > packages/config/README.md << 'EOF'
# @swarm/config

Shared configuration for Zerg and Jarvis.

## Usage

```typescript
import {
  modelsConfig,
  REALTIME_TIER_1,
  TEXT_TIER_1
} from '@swarm/config'

console.log(REALTIME_TIER_1) // 'gpt-4o-realtime-preview'
````

EOF

````

### Step 3: Update Root Workspace

```bash
# Add packages/* to workspaces
# Edit package.json to add "packages/*" to workspaces array
````

**Verify:**

```bash
bun install
bun run --filter @swarm/config --help
```

### Step 4: Update Consumers

#### Update `@jarvis/core/model-config.ts`

```typescript
// BEFORE
import modelsConfig from "../../../config/models.json";

// AFTER
import { modelsConfig, REALTIME_TIER_1, REALTIME_TIER_2 } from "@swarm/config";
```

#### Update Zerg Frontend (if it uses models.json)

```typescript
// BEFORE
import modelsConfig from "../../../config/models.json";

// AFTER
import { modelsConfig, TEXT_TIER_1 } from "@swarm/config";
```

#### Update Vitest Config (remove workaround)

```typescript
// apps/zerg/frontend-web/src/jarvis/vitest.config.ts
// REMOVE this line (no longer needed):
// '../../../config/models.json': resolve(__dirname, '../../../../config/models.json')
```

### Step 5: Create `packages/utils` (If Needed)

**Only if investigation found real duplication.**

```bash
mkdir -p packages/utils/src

cat > packages/utils/package.json << 'EOF'
{
  "name": "@swarm/utils",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "src/index.ts",
  "types": "src/index.ts"
}
EOF

# Add utilities found during investigation
cat > packages/utils/src/index.ts << 'EOF'
export * from './formatDate'
export * from './debounce'
// etc.
EOF
```

### Step 6: Create `packages/ui` (If Needed)

**Only if investigation found shared components.**

```bash
mkdir -p packages/ui/src

cat > packages/ui/package.json << 'EOF'
{
  "name": "@swarm/ui",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "src/index.ts",
  "types": "src/index.ts",
  "dependencies": {
    "react": "^19.2.1"
  },
  "peerDependencies": {
    "react": "^18.0.0 || ^19.0.0"
  }
}
EOF

# Add components
cat > packages/ui/src/Button.tsx << 'EOF'
export interface ButtonProps {
  onClick: () => void
  children: React.ReactNode
}

export function Button({ onClick, children }: ButtonProps) {
  return <button onClick={onClick}>{children}</button>
}
EOF
```

### Step 7: Test Everything

```bash
# Type check all packages
bun run type-check

# Run all tests
bun test

# Test Docker build
docker compose -f docker/docker-compose.yml --profile full build

# Test dev environment
make dev
```

### Step 8: Commit Incrementally

**DO NOT accumulate dozens of files before committing.**

Example commit sequence:

```bash
# Commit 1: Create packages/config
git add packages/config
git commit -m "feat: create @swarm/config package for shared model configuration"

# Commit 2: Update workspace
git add package.json
git commit -m "chore: add packages/* to workspace"

# Commit 3: Update consumers
git add apps/zerg/frontend-web/src/jarvis/core/model-config.ts
git commit -m "refactor: use @swarm/config in @jarvis/core"

# Commit 4: Remove Vitest workaround
git add apps/zerg/frontend-web/src/jarvis/vitest.config.ts
git commit -m "refactor: remove models.json alias workaround"

# etc.
```

---

## Success Criteria

Phase 2 is complete when:

✅ `packages/config` exists with typed exports of `models.json`
✅ `packages/utils` exists (if duplication was found) with shared TypeScript utilities
✅ `packages/ui` exists (if duplication was found) with shared React components
✅ Both Zerg and Jarvis import from `@swarm/*` packages
✅ All type checks pass
✅ All tests pass (174+ tests)
✅ Docker builds succeed
✅ `make dev` runs full stack successfully
✅ Vitest workaround removed (models.json alias no longer needed)

---

## Key Conventions & Gotchas

### Package Managers

- **JavaScript:** Bun ONLY. No npm/yarn
- **Python:** uv ONLY. No pip/poetry
- Run `bun install` from repo root (not in subdirs)

### Generated Code - DO NOT EDIT

- `apps/zerg/frontend-web/src/generated/` - OpenAPI types
- `apps/zerg/backend/zerg/ws_protocol/generated/` - WebSocket protocol
- Run `make generate-sdk` after API changes
- Run `make regen-ws` after WebSocket schema changes

### Docker Context

- Jarvis Dockerfiles expect repo root context
- Paths like `COPY apps/zerg/frontend-web/src/jarvis/...` not `COPY apps/...`
- Building from wrong directory will fail

### Imports

- Current: `../../../config/models.json` (brittle)
- Future: `@swarm/config` (clean)
- Fix import resolution in both vite configs and tsconfigs

### Testing While You Work

```bash
# Type check after every change
bun run type-check

# Run tests frequently
bun run test

# Test Docker build
docker compose -f docker/docker-compose.yml --profile full build

# Commit in increments
git status  # Don't let it grow to dozens of files
```

---

## Questions to Answer During Investigation

1. **Is there actually duplicated code?**
   - Survey both frontends thoroughly
   - If not much overlap, Phase 2 might just be `packages/config`

2. **What about styles/CSS?**
   - Are there shared design tokens?
   - Are there shared component styles?
   - Consider CSS-in-JS or shared CSS modules

3. **Should we create `packages/ui`?**
   - Only if there are truly reusable React components
   - Buttons, inputs, modals that look/behave the same
   - Don't create if components are domain-specific

4. **What about shared types?**
   - `Message`, `User`, `Session` types
   - API response types
   - Consider `packages/types` if there are many

---

## Files to Read First

```bash
# Understand current dependencies
cat apps/zerg/frontend-web/package.json
cat apps/zerg/frontend-web/src/jarvis/package.json

# See how models.json is currently used
cat apps/zerg/frontend-web/src/jarvis/core/model-config.ts

# Survey Zerg frontend structure
ls -R apps/zerg/frontend-web/src/

# Survey Jarvis frontend structure
ls -R apps/zerg/frontend-web/src/jarvis/src/
```

---

## Useful Commands Reference

```bash
# Start fresh dev environment
make stop && make dev

# Check logs
docker logs docker-jarvis-web-1
docker logs docker-frontend-web-1

# Check container status
docker ps --format "table {{.Names}}\t{{.Status}}"

# Validate contracts
make validate

# Regenerate types
make generate-sdk  # OpenAPI → TypeScript
make regen-ws      # AsyncAPI → WebSocket types

# Run specific test file
bun test packages/config

# Check what's in workspace
bun run --help
```

---

## Related Documentation

- **Phase 1 Results:** `docker/docker-compose.yml` (unified compose)
- **React Migration:** `apps/zerg/frontend-web/src/jarvis/MIGRATION.md`
- **Platform Overview:** `AGENTS.md`
- **E2E Testing:** `apps/zerg/frontend-web/src/jarvis/tests/E2E.md`
- **Git Log:** `git log --oneline -20` (see recent work)

---

## TL;DR

**Phase 1:** ✅ Docker consolidation complete
**Jarvis React Migration:** ✅ Complete and hardened (10 commits, all tests passing)
**Phase 2:** Create shared packages starting with `@swarm/config`

**First step:** Investigate what to share

```bash
cd apps/zerg/frontend-web/src && grep -r "export" src/ | head -50
cd apps/zerg/frontend-web/src/jarvis/src && grep -r "export" src/ | head -50
# Compare and document in PHASE_2_FINDINGS.md
```

**Required package:** `@swarm/config` for `models.json`
**Maybe packages:** `@swarm/utils` and `@swarm/ui` depending on findings

**Success:** Both apps import clean `@swarm/*` packages, all tests pass, Docker builds work.
