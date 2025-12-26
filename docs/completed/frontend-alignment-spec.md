# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Frontend Alignment Spec: Zerg Dashboard + Jarvis Chat

**Status:** Phases 1-3 Complete
**Date:** 2025-12-20
**Updated:** 2025-12-20

## Executive Summary

The Zerg dashboard and Jarvis chat UI were originally separate projects, now unified under the Swarmlet brand. They share the same backend and authentication but have divergent:
- Branding ("Swarmlet" vs "Jarvis AI")
- Navigation patterns
- Design token systems
- Visual aesthetics

This spec outlines a phased approach to align them as a cohesive product while preserving each UI's strengths.

---

## Current State Analysis

### Zerg Dashboard (`/dashboard`, `/canvas`, etc.)
| Aspect | Current State |
|--------|---------------|
| **Branding** | "Swarmlet" with logo |
| **Navigation** | Full tab bar (Chat, Dashboard, Canvas, Integrations, Runners, Admin) |
| **Aesthetic** | Professional dark - solid grays (#18181b surfaces) |
| **Tokens** | DTCG JSON → auto-generated CSS |
| **Components** | 29 React components |
| **CSS** | 11,500 lines, layer-based cascade |

### Jarvis Chat (`/chat`)
| Aspect | Current State |
|--------|---------------|
| **Branding** | "Jarvis AI" title |
| **Navigation** | Dashboard link button + Sync button only |
| **Aesthetic** | Cyber/sci-fi - glass morphism, animated backgrounds, neon accents |
| **Tokens** | Hand-written CSS custom properties |
| **Components** | 7 React components |
| **CSS** | 3,000 lines, modular imports |

### What They Share
- Same primary brand color: `#6366f1` (Electric Indigo)
- Same fonts: Inter (base), JetBrains Mono (code)
- Same intent colors: success/error/warning
- Same backend API + authentication
- Same nginx reverse proxy routing

---

## Recommended Approach

**Philosophy:** Align without homogenizing. The chat UI benefits from its focused, immersive aesthetic. The dashboard benefits from its information-dense, professional look. We want them to feel like the same app, not clone each other.

### Key Decisions

1. **Single Brand Name**: Swarmlet everywhere
2. **Unified Navigation Header**: Same structure, adapted styling per context
3. **Shared Token Foundation**: Common base tokens, context-specific extensions
4. **Preserve Aesthetics**: Dashboard stays solid, Chat keeps cyber/glass feel

---

## Phased Roadmap

### Phase 1: Brand Consistency (Quick Wins)
**Scope:** Unify naming without structural changes

**Changes:**
1. Rename "Jarvis AI" → "Swarmlet" in chat header
2. Add Swarmlet logo to chat header
3. Update PWA manifest name
4. Align favicon if different

**Files to modify:**
- `apps/zerg/frontend-web/src/jarvis/app/components/Header.tsx`
- `apps/zerg/frontend-web/public/site.webmanifest`
- `apps/zerg/frontend-web/index.html` (title tag)

---

### Phase 2: Navigation Alignment
**Scope:** Add consistent top-level navigation to chat UI

**Options:**

#### Option A: Shared Header Strip (Recommended)
Add a minimal global nav bar above the chat UI's existing header:
```
┌─────────────────────────────────────────────────┐
│ [Logo] Swarmlet    Chat │ Dashboard │ ...  [DE]│  ← Global nav (from Zerg)
├─────────────────────────────────────────────────┤
│            [Existing Jarvis header]             │  ← Context header
│         [Conversations]  │  [Chat Area]         │
│                          │                      │
└─────────────────────────────────────────────────┘
```

**Pros:**
- Clear navigation between app sections
- Preserves chat's immersive layout
- Users always know where they are

**Cons:**
- Adds vertical space overhead
- Two "headers" might feel redundant

#### Option B: Integrate into Existing Header
Expand Jarvis header to include navigation tabs:
```
┌─────────────────────────────────────────────────┐
│ [Logo] Swarmlet  │ Chat │ Dashboard │...│ [DE] │
├─────────────────────────────────────────────────┤
│ [Conversations]  │        [Chat Area]           │
└─────────────────────────────────────────────────┘
```

**Pros:**
- Single header, less vertical space
- Cleaner visual hierarchy

**Cons:**
- Requires more CSS work to match Jarvis aesthetic
- May feel cramped on mobile

#### Option C: Sidebar Navigation
Move global nav to a collapsible sidebar:
```
┌───┬──────────────────────────────────────────────┐
│ ☰ │                                              │
│   │         [Full Chat UI]                       │
│   │                                              │
└───┴──────────────────────────────────────────────┘
```

**Pros:**
- Maximum chat area
- Modern pattern (Discord, Slack)

**Cons:**
- Significant restructure of both UIs
- Inconsistent with current dashboard tabs

**Recommendation:** Start with **Option B** - integrate nav into Jarvis header. Simplest path to cohesion.

**Files to modify:**
- `apps/zerg/frontend-web/src/jarvis/app/components/Header.tsx`
- `apps/zerg/frontend-web/src/jarvis/styles/layout.css`

---

### Phase 3: Shared Design Token Foundation
**Scope:** Establish a shared token foundation both contexts consume

**Structure:**
```
apps/zerg/frontend-web/src/styles/tokens.css         # Shared tokens (single source of truth)
apps/zerg/frontend-web/src/styles/legacy.css         # Imports tokens.css (layers)
apps/zerg/frontend-web/src/jarvis/styles/base.css    # Imports ../../styles/tokens.css
```

**Token Merge Strategy:**
| Token Category | Shared? | Notes |
|----------------|---------|-------|
| Brand colors | Yes | Primary, secondary, accent |
| Intent colors | Yes | Success, error, warning |
| Font families | Yes | Inter, JetBrains Mono |
| Font sizes | Yes | Harmonize scales |
| Spacing | Yes | Use 4px base unit |
| Border radius | Yes | Same scale |
| Surface colors | **No** | Dashboard: solid, Chat: glass |
| Shadows | Partial | Base shadows shared, glows per-theme |
| Motion | Partial | Basic durations shared, complex animations per-theme |

**Migration Path:**
1. Keep shared tokens in `apps/zerg/frontend-web/src/styles/tokens.css`
2. Import tokens into both dashboard and Jarvis CSS entrypoints
3. Delete/avoid duplicate token definitions elsewhere

---

### Phase 4: Shared Component Library (Optional)
**Scope:** Extract common UI components to shared package

**Candidates for Sharing:**
- Button (primary, secondary, ghost variants)
- Input / TextArea
- Modal / Dialog
- Avatar
- Badge / Status indicator
- Toast notifications

**Not Worth Sharing:**
- Layout components (fundamentally different)
- Navigation (different patterns)
- Chat-specific components
- Dashboard-specific widgets

**Structure:**
```
Defer. Keep shared components in `apps/zerg/frontend-web/src/components/` for now.
```

**Recommendation:** Defer this phase. The UIs have few overlapping components, and the maintenance burden of a shared library may exceed benefits. Revisit when adding a third frontend or major features.

---

## Styling Guidelines (Post-Alignment)

### Dashboard Context
- Use solid surface colors (`--color-surface-section`, `--color-surface-card`)
- Minimal animations (respect `prefers-reduced-motion`)
- Dense information display
- Standard shadows, no glows

### Chat Context
- Use glass/transparent surfaces (`rgba()` with `backdrop-filter`)
- Immersive animations allowed (background grid, nebula)
- Conversational flow optimized
- Neon glow accents on interactive elements

### Shared Rules
- Same border radius scale everywhere
- Same spacing scale (4px base)
- Same font sizing scale
- Same brand color on primary actions
- Same focus states for accessibility

---

## Implementation Checklist

### Phase 1: Brand Consistency ✅
- [x] Update `Header.tsx` title to "Swarmlet"
- [x] Add logo to chat header (with cyber glow effect)
- [x] Update `manifest.json` name/short_name
- [x] Update `index.html` title tag
- [x] Copy logo asset to Jarvis public folder

### Phase 2: Navigation ✅
- [x] Implement nav tabs in Jarvis Header (Chat, Dashboard, Canvas, Integrations, Runners)
- [x] Style nav to match cyber aesthetic (glowing underline indicator)
- [x] Add active state indication (pulsing cyan/indigo glow)
- [x] Responsive: tabs shrink at 900px, hide at 640px
- [x] Brand logo links to /chat

### Phase 3: Tokens ✅
- [x] Create shared tokens in `apps/zerg/frontend-web/src/styles/tokens.css`
- [x] Import tokens into Jarvis via `apps/zerg/frontend-web/src/jarvis/styles/base.css`
- [x] Import tokens into dashboard via `apps/zerg/frontend-web/src/styles/legacy.css`

### Phase 4: Components (Deferred)
- [ ] Identify candidate components
- [ ] Create shared package structure
- [ ] Migrate Button component
- [ ] Migrate Input component
- [ ] Update imports in both apps

---

## Open Questions

1. **Mobile nav pattern?** Hamburger menu vs bottom nav vs drawer?
2. **Should chat sidebar be collapsible by default?** (Matches dashboard's shelf pattern)
3. **PWA install prompt?** Should both UIs prompt for install or just chat?
4. **Offline support?** Currently only chat has service worker - extend to dashboard?

---

## Success Metrics

- Users recognize both UIs as the same product
- Navigation between sections feels seamless
- No increase in CSS bundle size > 20%
- Accessibility audit passes (WCAG 2.1 AA)
- E2E tests pass for navigation flows

---

## Appendix: Current Token Comparison

| Token | Zerg Dashboard | Jarvis Chat |
|-------|----------------|-------------|
| Primary brand | `#6366f1` | `#6366f1` |
| Primary hover | `#4f46e5` | `#4f46e5` |
| Secondary | `#818cf8` | `#a855f7` (purple) |
| Page background | `#09090b` | `#030305` |
| Card surface | `#27272a` | `rgb(255 255 255 / 3%)` |
| Text primary | `#fafafa` | `#fff` |
| Text secondary | `#a1a1aa` | `#94a3b8` |
| Border subtle | `#27272a` | `rgb(255 255 255 / 5%)` |
| Success | `#10b981` | `#22c55e` |
| Error | `#ef4444` | `#ef4444` |
| Warning | `#f59e0b` | `#f59e0b` |
