# Frontend Effect-Boundary Cleanup

Status: In progress
Spec: `docs/specs/frontend-effect-boundary-cleanup.md`
Last updated: 2026-03-21

## Goal

Rewrite the obvious effect-heavy frontend surfaces so state has one owner, data fetching lives in React Query, and ordinary page behavior no longer depends on post-render synchronization.

## Current focus

- Rewrite `SessionsPage` and the remaining route-owned selection surfaces after landing `ChatPage`.
- Collapse remaining URL/list selection effects in the smaller pages after the two big route surfaces move.
- Keep pushing shared primitives only where they directly unlock effect deletion.

## Done when

- `ChatPage` and `SessionsPage` no longer use effects to synchronize URL state, local state, and navigation.
- `AuthProvider` and auth-method consumers derive from shared query state instead of mirrored local state.
- Manual `fetch -> setState` effects on core pages are replaced with query or mutation hooks.
- Repeated metadata/readiness/debounce patterns are centralized behind named hooks.
- A lint guardrail exists so new app-level state-sync effects do not creep back in.

## Checklist

- [x] Create persistent spec and tracking doc
- [x] Slice 1: add shared frontend primitives (`useAuthMethods`, metadata/readiness helpers, sanctioned debounce path)
- [x] Slice 2: rewrite auth/config consumers around shared query ownership
- [ ] Slice 3: rewrite route-owned pages (`ChatPage`, `SessionsPage`, conversation/trace/swarm/loop selection)
- [ ] Slice 4: move manual page fetch effects into React Query hooks
- [ ] Slice 5: clean up form/modal state choreography (`SettingsPage`, `SessionPickerModal`, related modals)
- [ ] Slice 6: collapse infra callback-sync effects and add lint ratchet

## Notes

- Audit snapshot on 2026-03-20: `160` direct `useEffect` calls across `66` frontend files; the largest concentration is in `pages/`.
- This task is not a vanity “fewer hooks” pass. Legitimate browser-sync effects stay; state-sync and fetch-sync effects are the target.
- Oikos voice/media hooks are explicitly not phase-one rewrite targets unless they block the simpler page/state cleanup.
- Slice 1 landed shared `useAuthMethods`, `usePageMeta`, `useReadinessFlag`, and `useDebouncedValue` primitives, plus first migrations and lint guardrails against direct page metadata/readiness writes.
- Slice 2 landed shared auth query keys, a query-owned `AuthProvider`, and cache updates that now flow through the same `current-user` contract instead of mirrored provider state.
- `ChatPage` now treats the route as the selected-thread source of truth; the remaining route-heavy rewrite is `SessionsPage` plus the smaller selection pages.
