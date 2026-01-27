# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Merge Oikos into Zerg Frontend

**Status:** Completed
**Date:** 2025-12-20
**Owner:** AI agent + David

> Note: This doc references the pre-merge `apps/zerg/frontend-web/src/oikos/...` layout. Oikos now lives in `apps/zerg/frontend-web/src/oikos/`.

## Goal

**One React app.** The Oikos chat UI becomes part of the Zerg frontend codebase. Not "embedded" or "linked" - literally the same source tree, same `node_modules`, same build.

### What this means
- `/chat` is a route in the Zerg React app
- Oikos components/hooks/utilities live in `apps/zerg/frontend-web/src/oikos/`
- No aliases, no volume mounts, no special resolution - just regular imports

### Non-goals
- Keeping Oikos as a separate deployable frontend
- Maintaining backwards compatibility with standalone Oikos

---

## Current State (before merge)

- **Zerg frontend:** `apps/zerg/frontend-web/` - React SPA with dashboard, canvas, settings
- **Oikos web:** `apps/zerg/frontend-web/src/oikos/` - Separate React app for chat UI
- **Oikos packages:** `apps/zerg/frontend-web/src/oikos/core/`, `apps/zerg/frontend-web/src/oikos/data/` - Shared utilities

Both apps have their own `package.json`, `node_modules`, build configs.

---

## Implementation Plan

### Phase 1: Copy Oikos Source into Zerg

**Move these into `apps/zerg/frontend-web/src/oikos/`:**

```
apps/zerg/frontend-web/src/oikos/src/        → apps/zerg/frontend-web/src/oikos/app/        (App.tsx, context/, hooks/, components/)
apps/zerg/frontend-web/src/oikos/lib/        → apps/zerg/frontend-web/src/oikos/lib/        (voice-controller, session-handler, etc.)
apps/zerg/frontend-web/src/oikos/styles/     → apps/zerg/frontend-web/src/oikos/styles/     (CSS files)
apps/zerg/frontend-web/src/oikos/core/   → apps/zerg/frontend-web/src/oikos/core/       (logger, client, model-config)
apps/zerg/frontend-web/src/oikos/data/ → apps/zerg/frontend-web/src/oikos/data/       (IndexedDB storage)
```

### Phase 2: Update Imports

- Change `@oikos/core` → `../core` (relative imports)
- Change `@oikos/data-local` → `../data` (relative imports)
- Change `@swarm/config` → inline or move config into oikos/

### Phase 3: Add Dependencies

Add to `apps/zerg/frontend-web/package.json`:
- `idb` (IndexedDB wrapper)
- `zod` (schema validation)
- Any other Oikos-specific deps not already in Zerg

### Phase 4: Wire Up Routes

- `OikosChatPage.tsx` imports from `./oikos/app/App`
- CSS loaded via standard import or the legacy injection hook
- `/chat` route already exists in App.tsx

### Phase 5: Cleanup

- Remove `apps/zerg/frontend-web/src/oikos/` (or archive)
- Remove Oikos from Docker Compose (no separate oikos-web service needed)
- Update nginx to remove `/chat/` legacy routing

---

## File Structure After Merge

```
apps/zerg/frontend-web/
├── src/
│   ├── components/          # Zerg shared components
│   ├── pages/
│   │   ├── DashboardPage.tsx
│   │   ├── OikosChatPage.tsx  # Entry point for chat
│   │   └── ...
│   ├── oikos/              # Former Oikos codebase
│   │   ├── app/             # React app (App.tsx, context, hooks, components)
│   │   ├── lib/             # Utilities (voice, session, audio, etc.)
│   │   ├── core/            # Core utilities (logger, client)
│   │   ├── data/            # IndexedDB storage
│   │   └── styles/          # Oikos CSS
│   ├── styles/              # Zerg styles
│   └── routes/
│       └── App.tsx          # Router with /chat route
└── package.json             # Single package.json with all deps
```

---

## Acceptance Criteria

1. `bun run build` produces one bundle containing both Zerg and Oikos UI
2. `/chat` renders the chat interface
3. `/dashboard` renders the dashboard
4. Navigation between them has no full page reload
5. Chat can send messages and show worker progress
6. No Docker volume mounts or Vite aliases needed

---

## Notes

- The standalone Oikos PWA (`/chat/`) will be deprecated after merge
- Voice/WebRTC functionality should continue to work
- E2E tests may need updating to use new paths
