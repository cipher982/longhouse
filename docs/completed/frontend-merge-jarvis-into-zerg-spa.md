# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Merge Jarvis into Zerg Frontend

**Status:** Completed
**Date:** 2025-12-20
**Owner:** AI agent + David

> Note: This doc references the pre-merge `apps/zerg/frontend-web/src/jarvis/...` layout. Jarvis now lives in `apps/zerg/frontend-web/src/jarvis/`.

## Goal

**One React app.** The Jarvis chat UI becomes part of the Zerg frontend codebase. Not "embedded" or "linked" - literally the same source tree, same `node_modules`, same build.

### What this means
- `/chat` is a route in the Zerg React app
- Jarvis components/hooks/utilities live in `apps/zerg/frontend-web/src/jarvis/`
- No aliases, no volume mounts, no special resolution - just regular imports

### Non-goals
- Keeping Jarvis as a separate deployable frontend
- Maintaining backwards compatibility with standalone Jarvis

---

## Current State (before merge)

- **Zerg frontend:** `apps/zerg/frontend-web/` - React SPA with dashboard, canvas, settings
- **Jarvis web:** `apps/zerg/frontend-web/src/jarvis/` - Separate React app for chat UI
- **Jarvis packages:** `apps/zerg/frontend-web/src/jarvis/core/`, `apps/zerg/frontend-web/src/jarvis/data/` - Shared utilities

Both apps have their own `package.json`, `node_modules`, build configs.

---

## Implementation Plan

### Phase 1: Copy Jarvis Source into Zerg

**Move these into `apps/zerg/frontend-web/src/jarvis/`:**

```
apps/zerg/frontend-web/src/jarvis/src/        → apps/zerg/frontend-web/src/jarvis/app/        (App.tsx, context/, hooks/, components/)
apps/zerg/frontend-web/src/jarvis/lib/        → apps/zerg/frontend-web/src/jarvis/lib/        (voice-controller, session-handler, etc.)
apps/zerg/frontend-web/src/jarvis/styles/     → apps/zerg/frontend-web/src/jarvis/styles/     (CSS files)
apps/zerg/frontend-web/src/jarvis/core/   → apps/zerg/frontend-web/src/jarvis/core/       (logger, client, model-config)
apps/zerg/frontend-web/src/jarvis/data/ → apps/zerg/frontend-web/src/jarvis/data/       (IndexedDB storage)
```

### Phase 2: Update Imports

- Change `@jarvis/core` → `../core` (relative imports)
- Change `@jarvis/data-local` → `../data` (relative imports)
- Change `@swarm/config` → inline or move config into jarvis/

### Phase 3: Add Dependencies

Add to `apps/zerg/frontend-web/package.json`:
- `idb` (IndexedDB wrapper)
- `zod` (schema validation)
- Any other Jarvis-specific deps not already in Zerg

### Phase 4: Wire Up Routes

- `JarvisChatPage.tsx` imports from `./jarvis/app/App`
- CSS loaded via standard import or the legacy injection hook
- `/chat` route already exists in App.tsx

### Phase 5: Cleanup

- Remove `apps/zerg/frontend-web/src/jarvis/` (or archive)
- Remove Jarvis from Docker Compose (no separate jarvis-web service needed)
- Update nginx to remove `/chat/` legacy routing

---

## File Structure After Merge

```
apps/zerg/frontend-web/
├── src/
│   ├── components/          # Zerg shared components
│   ├── pages/
│   │   ├── DashboardPage.tsx
│   │   ├── JarvisChatPage.tsx  # Entry point for chat
│   │   └── ...
│   ├── jarvis/              # Former Jarvis codebase
│   │   ├── app/             # React app (App.tsx, context, hooks, components)
│   │   ├── lib/             # Utilities (voice, session, audio, etc.)
│   │   ├── core/            # Core utilities (logger, client)
│   │   ├── data/            # IndexedDB storage
│   │   └── styles/          # Jarvis CSS
│   ├── styles/              # Zerg styles
│   └── routes/
│       └── App.tsx          # Router with /chat route
└── package.json             # Single package.json with all deps
```

---

## Acceptance Criteria

1. `bun run build` produces one bundle containing both Zerg and Jarvis UI
2. `/chat` renders the chat interface
3. `/dashboard` renders the dashboard
4. Navigation between them has no full page reload
5. Chat can send messages and show worker progress
6. No Docker volume mounts or Vite aliases needed

---

## Notes

- The standalone Jarvis PWA (`/chat/`) will be deprecated after merge
- Voice/WebRTC functionality should continue to work
- E2E tests may need updating to use new paths
