# Add Context Modal

**Status:** In Progress
**Date:** 2025-12-31
**Parent Spec:** user-context-v3.md

## Executive Summary

A frontend modal that lets users add context documents to their knowledge base via:
1. File upload (drag/drop or browse)
2. Free-form text paste

Backend: Uses existing Knowledge Base API (`POST /api/knowledge/sources` + sync).

## Decision Log

### Decision: Separate docs vs single growing doc
**Context:** Should users create multiple docs or append to one?
**Choice:** Multiple separate docs
**Rationale:** Better searchability, easier to manage/delete, matches mental model ("my servers" vs "my recipes")
**Revisit if:** Users complain about too many docs

### Decision: Modal location
**Context:** Where does the modal get triggered from?
**Choice:** Dashboard header + Knowledge page + empty state CTA
**Rationale:** Multiple entry points for discoverability
**Revisit if:** Analytics show one entry point dominates

### Decision: File types supported
**Context:** What files can be uploaded?
**Choice:** Phase 1: .txt, .md only. Phase 2: .pdf, .csv, .docx
**Rationale:** Start simple, text extraction for other formats needs backend work
**Revisit if:** Users request other formats

## Architecture

```
┌─────────────────────────────────────────┐
│         AddContextModal.tsx             │
│  ┌─────────────────────────────────┐    │
│  │  Tab: Upload | Paste            │    │
│  ├─────────────────────────────────┤    │
│  │  [Title input]                  │    │
│  │  [Content area / Drop zone]     │    │
│  │  [Save button]                  │    │
│  ├─────────────────────────────────┤    │
│  │  Existing docs list (count)     │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────┐
│  Existing KB API                        │
│  POST /api/knowledge/sources            │
│  { name, source_type: "user_text",      │
│    config: { content: "..." } }         │
│  → auto-creates doc on save             │
└─────────────────────────────────────────┘
```

## Implementation Phases

### Phase 1: Modal UI Component
**Goal:** Working modal with tabs, form inputs, styling. Mock submit (console.log).

**Acceptance Criteria:**
- [ ] Modal component renders with open/close
- [ ] Two tabs: "Upload File" and "Paste Text"
- [ ] Paste tab: Title input + textarea + Save button
- [ ] Upload tab: Drop zone + file picker + title auto-fill from filename
- [ ] Form validation (title required, content required)
- [ ] Loading state on submit
- [ ] Success state clears form for another entry
- [ ] Shows count of existing docs (mocked)
- [ ] Responsive (works on mobile)
- [ ] Matches existing Zerg/Swarmlet design system

**Test Commands:**
```bash
make dev  # Start dev server
# Navigate to /dashboard, click "Add Context" button
# Verify modal opens, tabs work, form validates
```

### Phase 2: API Integration
**Goal:** Wire modal to real Knowledge Base API.

**Acceptance Criteria:**
- [ ] Paste submit calls `POST /api/knowledge/sources` with source_type "user_text"
- [ ] File upload reads file content, submits same way
- [ ] Error handling shows user-friendly message
- [ ] Success triggers knowledge source sync
- [ ] Existing docs count fetches from `GET /api/knowledge/sources`
- [ ] New doc appears in Knowledge page list after save
- [ ] Works in production build

**Test Commands:**
```bash
make dev
# Add a doc via modal
# Verify it appears in GET /api/knowledge/sources
# Verify knowledge_search finds the content
```

### Phase 3: Entry Points (stretch)
**Goal:** Add modal triggers to multiple locations.

**Acceptance Criteria:**
- [ ] Dashboard header has "Add Context" button
- [ ] Knowledge page has "Add Context" button
- [ ] Empty state on Knowledge page prompts to add first doc
- [ ] Chat page has subtle "Add context" link

## Files to Create/Modify

**Create:**
- `apps/zerg/frontend-web/src/components/AddContextModal.tsx`
- `apps/zerg/frontend-web/src/components/AddContextModal.css`

**Modify:**
- `apps/zerg/frontend-web/src/pages/KnowledgeSourcesPage.tsx` - add button
- `apps/zerg/backend/zerg/routers/knowledge.py` - add "user_text" source type (if needed)

## Non-Goals

- PDF/CSV parsing (Phase 2 of larger roadmap)
- Drag-drop reordering of docs
- Folders/organization
- Sharing docs between users
- Rich text editor (markdown only)
