# Deployment Architecture Cleanup Plan

## Status (December 2025)

### ✅ Documentation Cleanup Complete
- Removed duplicate Monitoring section from DEPLOYMENT.md
- Removed DEPRECATED api subdomain section
- Trimmed Manual Deployment (Option 2) to a note
- Added Coolify API docs to COOLIFY_DEBUGGING.md (architecture, deploy trigger)
- Consolidated AGENTS.md as primary quick reference for agents

### Remaining Decisions (Optional)
The Docker hacks below are **pragmatic and working**. Clean up only if they become painful.

---

## Current State Analysis

### The ONE Real Bug (Fixed)
- `.gitignore` had `data/` → excluded `apps/zerg/frontend-web/src/jarvis/data/` source files
- **Fixed in a2f3bcc:** Changed to `/data/` (root-level only)

### Good Changes (Keep)
- Design tokens as workspace dependency
- Frontend Docker uses repo root context (consistent with backend)
- Removed copy-tokens.mjs hack
- CSS imports from package

### Hacks/Workarounds (Current)
1. **Generated package.json in Dockerfile** - creates minimal workspace config with echo
2. **Committed design tokens dist/** - violates "no generated files" principle
3. **Simplified nginx.dockerfile** - removed formatting to avoid Coolify parser issues

## The Fundamental Problem

**Docker + Bun Workspaces:**
- Root `package.json` declares: `["apps/zerg/frontend-web", "apps/zerg/e2e", "apps/runner", "packages/*"]`
- Docker only copies: `packages/design-tokens` + `apps/zerg/frontend-web`
- Bun sees package.json → expects ALL workspaces → fails

**Current hack:** Generate minimal package.json inline
**Proper solution:** Use turbo prune

## Recommended Cleanup (From First Principles)

### Option A: Turbo Prune (Industry Standard) ⭐

**What it does:**
- `turbo prune frontend-web --docker` generates pruned workspace
- Creates `out/json/` (minimal package.json + lockfile)
- Creates `out/full/` (only dependencies needed)
- Zero hacks, automated, scales with repo growth

**Implementation:**
```dockerfile
FROM node:alpine AS pruner
RUN npm install -g turbo
COPY . .
RUN turbo prune zerg-frontend-web --docker

FROM oven/bun:alpine AS builder
COPY --from=pruner /app/out/json/ .
COPY --from=pruner /app/out/full/ .
RUN bun install
RUN cd packages/design-tokens && bun run build
WORKDIR /app/apps/zerg/frontend-web
RUN bun run build
```

**Pros:**
- No manual package.json manipulation
- Proper monorepo pattern (Vercel uses this)
- Scales as repo grows
- No committed generated files

**Cons:**
- Adds turbo dependency
- Extra build stage (adds ~10s)

### Option B: Keep Current (Documented Pragmatism)

Accept the generated package.json as documented tech debt:
- Add comment explaining why
- Works reliably
- Simple to understand
- Can refactor later when it becomes painful

**Pros:**
- Zero new dependencies
- Works now
- Clear and explicit

**Cons:**
- Hack that needs maintenance if workspaces change
- Not industry standard

### Option C: Copy All Workspaces (Simple but Wasteful)

Add `.dockerignore` entries to exclude node_modules from copied workspaces:
```
apps/zerg/e2e/node_modules/**
apps/zerg/e2e/.playwright/**
```

**Pros:**
- No package.json manipulation
- Simple

**Cons:**
- Still copies 1.1GB e2e directory (minus node_modules = ~50MB)
- Wastes build cache

## Design Tokens: Keep Committed or Build Fresh?

**Current:** Committed (17KB)

**Arguments for keeping committed:**
- Tiny (17KB)
- Rarely change
- One less build step
- Solo dev - no merge conflicts

**Arguments for building fresh:**
- Principle: don't commit generated files
- True reproducibility
- Source of truth is tokens.json

**Recommendation:** Keep committed for now (pragmatic), revisit if tokens become large or change frequently.

## Nginx Dockerfile

The simplification was unnecessary - the original was fine. Can restore formatting.

## Final Recommendation

**For "perfect codebase from first principles":**
1. Implement turbo prune (Option A)
2. Remove generated package.json hack
3. Remove committed tokens (build fresh)
4. Restore nginx.dockerfile formatting

**For "pragmatic solo dev":**
1. Keep current setup (documented)
2. Maybe restore nginx formatting
3. Keep committed tokens
4. Revisit when repo scales or team grows

## Implementation Priority

If going with turbo prune approach:
1. Add turbo to devDependencies
2. Update frontend Dockerfile to use prune
3. Remove generated package.json
4. Optionally: remove committed tokens, add build step back
5. Test locally
6. Deploy
