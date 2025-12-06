# Landing Page Professionalization - Master Task Document

**Created:** 2025-12-02
**Updated:** 2025-12-02
**Status:** Phase 1 & 2 Complete
**Goal:** Transform the landing page from a functional MVP to a professional, trustworthy product site

---

## Overview

This document tracks the implementation of landing page improvements identified in the site audit. The primary objectives are:

1. Fix all broken footer links by creating real destination pages
2. Replace placeholder visuals with authentic product screenshots
3. Build a trust center to substantiate security claims
4. Add pricing transparency

---

## Task Categories

### Phase 1: Fix Broken Navigation (Critical) ✅ COMPLETE

Routes that currently dead-end back to landing page.

| #   | Task                                               | Status  | File(s)             | Notes                                  |
| --- | -------------------------------------------------- | ------- | ------------------- | -------------------------------------- |
| 1.1 | Create `/pricing` page with pricing tiers          | ✅ Done | `PricingPage.tsx`   | Free beta + Pro/Enterprise coming soon |
| 1.2 | Create `/docs` page (or redirect to external docs) | ✅ Done | `DocsPage.tsx`      | Quick start guide with API docs        |
| 1.3 | Create `/changelog` page                           | ✅ Done | `ChangelogPage.tsx` | Version history with badges            |
| 1.4 | Create `/privacy` policy page                      | ✅ Done | `PrivacyPage.tsx`   | Full privacy policy content            |
| 1.5 | Create `/security` page (trust center)             | ✅ Done | `SecurityPage.tsx`  | Architecture, roadmap, disclosure      |
| 1.6 | Register all new routes in `App.tsx`               | ✅ Done | `App.tsx`           | Public routes, no auth required        |
| 1.7 | Update `FooterCTA.tsx` links to use correct paths  | ✅ Done | `FooterCTA.tsx`     | Using React Router Link                |
| 1.8 | Add `#pricing` anchor or link to pricing page      | ✅ Done | `FooterCTA.tsx`     | Links to /pricing page                 |

### Phase 2: Trust & Credibility ✅ COMPLETE

Build substantiation for security/compliance claims.

| #   | Task                                     | Status  | File(s)            | Notes                             |
| --- | ---------------------------------------- | ------- | ------------------ | --------------------------------- |
| 2.1 | Design trust center page layout          | ✅ Done | `SecurityPage.tsx` | Highlights, architecture, roadmap |
| 2.2 | Write security practices content         | ✅ Done | Content            | Encryption, auth, logging details |
| 2.3 | Link TrustSection badges to trust center | ✅ Done | `TrustSection.tsx` | SVG icons + link to /security     |
| 2.4 | Add compliance roadmap (SOC 2, etc.)     | ✅ Done | `SecurityPage.tsx` | Visual roadmap with status badges |

### Phase 3: Visual Authenticity

Replace placeholder illustrations with real product.

| #   | Task                                          | Status     | File(s)                | Notes                     |
| --- | --------------------------------------------- | ---------- | ---------------------- | ------------------------- |
| 3.1 | Capture real `/canvas` screenshot             | ⬜ Pending | Assets                 | React Flow visual builder |
| 3.2 | Capture real `/dashboard` screenshot          | ⬜ Pending | Assets                 | Runtime data view         |
| 3.3 | Update NerdSection with real canvas image     | ⬜ Pending | `NerdSection.tsx`      | Replace placeholder       |
| 3.4 | Update ScenariosSection with relevant visuals | ⬜ Pending | `ScenariosSection.tsx` | Consider video/GIF        |
| 3.5 | Create/add product demo video                 | ⬜ Pending | HeroSection            | "See it in action" CTA    |

### Phase 4: Content Accuracy

Ensure marketing claims match reality.

| #   | Task                                                  | Status     | File(s)                   | Notes                         |
| --- | ----------------------------------------------------- | ---------- | ------------------------- | ----------------------------- |
| 4.1 | Audit IntegrationsSection against real connectors     | ⬜ Pending | `IntegrationsSection.tsx` | Mark unavailable integrations |
| 4.2 | Add "coming soon" labels to aspirational integrations | ⬜ Pending | `IntegrationsSection.tsx` | Visual differentiation        |
| 4.3 | Review PASSection claims for accuracy                 | ⬜ Pending | `PASSection.tsx`          | Ensure deliverable promises   |

### Phase 5: Polish & Optimization

Final touches for professional presentation.

| #   | Task                                      | Status     | File(s)              | Notes                    |
| --- | ----------------------------------------- | ---------- | -------------------- | ------------------------ |
| 5.1 | Add consistent page transitions           | ⬜ Pending | CSS                  | Smooth navigation feel   |
| 5.2 | Create shared layout for legal/info pages | ⬜ Pending | `InfoPageLayout.tsx` | Consistent header/footer |
| 5.3 | Add meta tags for new pages (SEO)         | ⬜ Pending | Each page            | Title, description       |
| 5.4 | Test all navigation paths                 | ⬜ Pending | Manual QA            | No dead ends             |

---

## Implementation Order

### Immediate (Session 1)

1. **1.1-1.5**: Create all missing page stubs with minimal content
2. **1.6**: Register routes in App.tsx
3. **1.7-1.8**: Fix footer links

### Follow-up (Session 2)

4. **2.1-2.4**: Build out trust center content
5. **3.1-3.3**: Capture and integrate real product screenshots

### Polish (Session 3)

6. **4.1-4.3**: Content accuracy audit
7. **5.1-5.4**: Final polish

---

## Technical Decisions

### Page Structure Pattern

All new public pages will follow this pattern:

```tsx
export default function PageNamePage() {
  return (
    <div className="info-page">
      <header className="info-page-header">
        <Link to="/">← Back to Home</Link>
        <h1>Page Title</h1>
      </header>
      <main className="info-page-content">{/* Content */}</main>
      <footer className="info-page-footer">
        {/* Minimal footer with copyright */}
      </footer>
    </div>
  );
}
```

### CSS Organization

- New styles in `src/styles/info-pages.css`
- Import into pages that need it
- Reuse existing design tokens

### Routing Approach

- All new pages are **public** (no auth required)
- Added at root level in App.tsx alongside LandingPage
- Wrapped in ErrorBoundary for consistency

---

## Content Requirements

### Pricing Page

- Free tier details (current offering)
- Future paid tiers (placeholder or "coming soon")
- Feature comparison table
- CTA to sign up

### Privacy Policy

- Data collection practices
- Data storage and retention
- User rights (access, deletion)
- Contact information
- Last updated date

### Security Page (Trust Center)

- Architecture overview (high-level)
- Authentication method (Google OAuth)
- Data encryption practices
- Compliance roadmap
- Responsible disclosure policy

### Changelog

- Version history format
- Recent updates
- Link to GitHub releases if applicable

### Docs

- Decision: Redirect to external docs OR simple getting started guide
- If internal: Quick start, API overview, FAQ

---

## Progress Log

| Date       | Tasks Completed  | Notes                                                      |
| ---------- | ---------------- | ---------------------------------------------------------- |
| 2025-12-02 | Document created | Initial planning                                           |
| 2025-12-02 | Phase 1 complete | All 5 pages created, routes registered, footer links fixed |
| 2025-12-02 | Phase 2 complete | Trust center with roadmap, badges linked to /security      |

---

## File Locations Reference

```
apps/zerg/frontend-web/src/
├── pages/
│   ├── LandingPage.tsx          # Main landing
│   ├── PricingPage.tsx          # NEW
│   ├── DocsPage.tsx             # NEW
│   ├── ChangelogPage.tsx        # NEW
│   ├── PrivacyPage.tsx          # NEW
│   └── SecurityPage.tsx         # NEW (Trust Center)
├── components/
│   └── landing/
│       ├── FooterCTA.tsx        # Fix links
│       ├── TrustSection.tsx     # Add trust center links
│       ├── NerdSection.tsx      # Update visuals
│       ├── ScenariosSection.tsx # Update visuals
│       └── IntegrationsSection.tsx # Audit accuracy
├── routes/
│   └── App.tsx                  # Add new routes
└── styles/
    ├── landing.css              # Existing styles
    └── info-pages.css           # NEW shared styles
```
