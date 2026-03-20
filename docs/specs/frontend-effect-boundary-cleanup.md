# Frontend Effect-Boundary Cleanup

Status: In progress

## Goal

Make the frontend cheap to reason about again:

- state should have one obvious owner
- data fetching should live in React Query, not ad hoc `useEffect`
- route state should come from the router, not post-render sync
- effects should mostly mean “sync with the browser or an external system”

This is a cleanup and rewrite pass, not a small lint tweak. The point is to remove whole categories of timing-based behavior from the pages we touch most.

## Problem Statement

The current frontend has too many places where `useEffect` is acting as control flow:

- route params copied into local state, then pushed back into the URL
- query results mirrored into separate component state
- page fetches done as `fetch -> setLoading -> setState -> cancelled flag`
- form data hydrated from server state through effects, then manually reset through duplicate code
- repeated metadata, readiness, and debounce logic open-coded in many files

That creates three costs:

1. pages become hard to read because the real behavior is spread across handlers, render logic, and several effects
2. refactors become brittle because dependency arrays hide coupling
3. agents will keep extending the same patterns unless we replace them with clearer primitives

## Principles

### 1. Effects are for external synchronization

Direct effects are acceptable when they clearly synchronize with:

- DOM APIs
- browser lifecycle or events
- storage
- WebSocket or subscription lifecycles
- media, voice, or third-party widget setup

If an effect is really coordinating app state, it is suspect by default.

### 2. Derived state stays in render

Do not store values that can be derived from:

- props
- query results
- route params
- other local state

If a component needs `selectedId`, `selectedItem`, and `selectedIndex`, one of those should usually be the real state and the rest should be derived.

### 3. Data fetching belongs to React Query

Do not do routine page fetches in component effects when React Query can own:

- loading state
- error state
- retries
- cancellation
- cache invalidation
- conditional enablement

### 4. Route state is a source of truth

If a page is identified by URL state, treat the URL as authoritative. Avoid “URL -> local state” sync effects and “local state -> URL” sync effects running in parallel.

### 5. Resets should be structural or explicit

If changing an entity should reset the page or form, prefer:

- keyed remounts
- reducer resets
- explicit event handlers

Do not rely on dependency-array choreography to manually zero every field after render.

### 6. Shared cross-page browser behavior gets a named hook

Repeated imperative behavior should not be reimplemented per page. Common cases:

- page metadata
- readiness flags
- auth-method fetches
- debounced values

## Non-Goals

This pass does **not**:

- ban every direct `useEffect`
- rewrite the Oikos voice/media stack just to reduce counts
- change visual design for its own sake
- replace React Query or React Router with other state libraries
- optimize for minimum churn over clarity; we are explicitly willing to rewrite the clear winners

## Audit Baseline

The initial audit found:

- `160` direct `useEffect` or `React.useEffect` calls
- `66` frontend files using direct effects
- the heaviest concentration in `pages/`, then `components/`, then shared `lib/`

Clear winners for rewrite:

- `pages/ChatPage.tsx`
- `pages/SessionsPage.tsx`
- `lib/auth.tsx`
- `pages/SettingsPage.tsx`
- `pages/ConversationsPage.tsx`
- `pages/TraceExplorerPage.tsx`
- `pages/SwarmOpsPage.tsx`
- `pages/LoopInboxPage.tsx`
- `pages/OikosChatPage.tsx`
- `pages/SessionDetailPage.tsx`
- `components/SessionPickerModal.tsx`
- `components/AddKnowledgeSourceModal.tsx`
- `lib/useWebSocket.tsx`

## Rewrite Slices

### Slice 1: Shared Primitives and Guardrails

Add the primitives that later slices will migrate onto:

- `useAuthMethods()` backed by React Query
- `usePageMeta()` for `document.title` and description updates
- `useReadinessFlag()` for `data-ready` / `data-screenshot-ready`
- one sanctioned debounce path:
  - prefer `useDeferredValue` for query/filter UI where it fits
  - otherwise add one shared `useDebouncedValue()` hook instead of repeating timeout effects
- a staged lint rule or import restriction so new app-level state-sync effects do not get added casually

Files immediately touched in this slice:

- `apps/zerg/frontend-web/src/lib/auth.tsx`
- public info pages currently open-coding page metadata
- readiness-flag pages currently open-coding body attribute mutations

### Slice 2: Auth and Config Query Ownership

Rewrite auth/config consumers so query state owns auth truth:

- `AuthProvider` should derive `user` and `isAuthenticated` from the `current-user` query instead of mirroring into local state
- `LoginOverlay` and `Layout` should consume shared auth-method query state instead of each fetching `getAuthMethods()` manually
- Google sign-in script setup can stay effect-based, but should be isolated behind a clearly named browser-integration helper

Primary files:

- `apps/zerg/frontend-web/src/lib/auth.tsx`
- `apps/zerg/frontend-web/src/components/Layout.tsx`

### Slice 3: Route-Owned State Rewrites

Remove pages where URL state, local state, and effects are currently fighting each other.

#### 3a. Chat page

`ChatPage` should treat the route as the thread selector. Rewrite away:

- URL -> local `selectedThreadId` sync
- navigation sync effect
- auto-select effect
- auto-create thread effect

Preferred end state:

- route params identify the active thread
- thread creation happens in an explicit handler or route bootstrap boundary
- the page renders from route/query data without post-render synchronization

#### 3b. Sessions page

`SessionsPage` should stop maintaining parallel local filter state and URL state.

Preferred end state:

- filters read from URL-backed state helpers
- updating a filter updates the source directly
- pagination resets happen in the setter path or as a derived query key, not a side effect
- debounce path is shared rather than open-coded

#### 3c. Smaller route-state pages

Remove selection-sync effects from:

- `ConversationsPage`
- `TraceExplorerPage`
- `SwarmOpsPage`
- `LoopInboxPage`
- `legacy/forum/ForumPage.tsx` if still active enough to justify cleanup

Preferred end state:

- selected entity comes from route params or a single local state source
- fallback navigation is explicit and minimal

### Slice 4: Query Ownership for Manual Fetch Effects

Move routine page fetches into React Query hooks.

Targets:

- `SessionDetailPage` turn telemetry fetch
- `SessionChat` lock check
- `OikosChatPage` capabilities check and thread preload
- any remaining page/component code doing manual `fetch -> setState` for ordinary app data

Preferred end state:

- `enabled` controls fetch timing
- loading and error state come from queries
- cancellations and retries are handled by the query layer

### Slice 5: Form and Modal State Cleanup

Rewrite the places where effects are compensating for awkward local state shape.

#### 5a. Settings page

Current smell:

- query data hydrates many local fields through an effect
- reset logic duplicates the same field assignments

Preferred end state:

- keyed form remount or reducer-based form state
- one reset path
- no “copy server object into eight state atoms after render” effect

#### 5b. Session picker modal

Current smell:

- multiple effects for debounce, selection reset, open-state reset, and focus timing

Preferred end state:

- selected session identity is the real state
- selected index is derived
- modal body resets structurally when opened for a new request

#### 5c. Add knowledge source modal

Current smell:

- page-by-page repo accumulation stored in component state through an effect

Preferred end state:

- use `useInfiniteQuery`
- flatten pages in render

#### 5d. Add runner modal

Current smell:

- opening the modal is used as a trigger for a mutation

Preferred end state:

- token creation happens in the open handler, or in a keyed mount-only modal body with explicit lifecycle semantics

### Slice 6: Shared Infra Cleanup

Clean up the effect-heavy shared primitives once page rewrites have reduced pressure.

#### 6a. WebSocket hook

`useWebSocket.tsx` currently uses several effects only to mirror callbacks into refs and also has duplicated lifecycle cleanup.

Preferred end state:

- use `useEffectEvent` or an equivalent internal stable-callback abstraction
- a single connection lifecycle effect
- one cleanup path

#### 6b. Readiness and metadata adoption

After helpers exist, migrate repeated pages onto them and delete bespoke implementations.

#### 6c. Debounce consolidation

Replace repeated timeout-based local implementations in:

- `SessionsPage`
- `SessionPickerModal`
- `RecallPanel`
- `KnowledgeSearchPanel`
- `useSessionWorkspace`
- `usePerformance`

with one sanctioned shared pattern.

## Out of Scope but Adjacent

- Oikos voice/media lifecycle hooks unless they become blockers
- low-level DOM integration that is already a clean external-sync effect
- SSR or head-management architecture changes beyond a simple shared metadata hook

## Acceptance Criteria

- `ChatPage` has no effects whose job is to synchronize local thread state with route state or navigation
- `SessionsPage` has no effects whose job is to synchronize filter state with URL state or reset pagination after the fact
- `AuthProvider` no longer mirrors query auth data into independent local truth
- repeated auth-method fetching is replaced by one shared query hook
- core manual page fetch effects are replaced by query or mutation hooks
- page metadata and readiness flags use shared helpers instead of repeated open-coded effects
- a lint guardrail exists for newly migrated surfaces so state-sync effects do not reappear
- `make test` and the relevant frontend unit coverage pass on the final tree

## Execution Order

1. Land shared primitives and auth-method query consolidation
2. Rewrite `ChatPage` and `SessionsPage`
3. Rewrite auth state ownership
4. Move manual fetch effects on core pages into query hooks
5. Rewrite form/modal winners
6. Ratchet with lint and shared infra cleanup

## Notes

- This spec intentionally optimizes for readability and future change safety over minimal diff size.
- “No direct useEffect” is not the product requirement. “No hidden state choreography” is.
