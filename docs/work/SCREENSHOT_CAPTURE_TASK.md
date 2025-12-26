# Landing Page Screenshot Capture & Validation

**Created:** 2025-12-25
**Assignee:** [Other Dev]
**Status:** Ready for capture
**Estimated Time:** 30-60 minutes

---

## Background

We've completed the landing page polish (Phases 3-5). The visual components are built and ready, but they're currently showing placeholder content. This task is to capture real product screenshots and swap them in.

### What Was Built

| Component | Location | Purpose |
|-----------|----------|---------|
| `AppScreenshotFrame` | `src/components/landing/AppScreenshotFrame.tsx` | macOS-style window chrome wrapper for screenshots |
| `DemoVideoPlaceholder` | `src/components/landing/DemoVideoPlaceholder.tsx` | Video player placeholder with "Coming Soon" state |
| `DemoSection` | `src/components/landing/DemoSection.tsx` | New section for product demo video |

### Current State

- **NerdSection**: Has `AppScreenshotFrame` wrapping an existing canvas illustration (`/images/landing/canvas-preview.png`)
- **ScenariosSection**: Uses `AppScreenshotFrame` for scenario card images
- **DemoSection**: Shows "Demo Video Coming Soon" placeholder

---

## Task 1: Capture Screenshots

### 1.1 Visual Workflow Builder (Canvas)

**Target:** Replace the illustration in NerdSection with a real canvas screenshot

**Steps:**
1. Start the dev server: `make dev`
2. Navigate to: `http://localhost:30080/canvas`
3. Create a visually interesting workflow:
   - Add a Trigger node (e.g., "New Email")
   - Add an AI Agent node (e.g., "Analyze Sentiment")
   - Add a Condition node (e.g., "Is Urgent?")
   - Add Action nodes on both branches (e.g., "Send Slack Alert" / "Archive Email")
   - Connect them with edges
4. Zoom/pan to frame the workflow nicely (not too zoomed in/out)
5. Take screenshot: **1920x1080** or **2560x1440** (16:9 aspect ratio)
6. Save as: `apps/zerg/frontend-web/public/images/landing/canvas-screenshot.png`

**Quality Checklist:**
- [ ] Nodes are clearly visible with readable labels
- [ ] Connections/edges are visible
- [ ] Good contrast (dark theme should look good)
- [ ] No personal/sensitive data visible
- [ ] Sidebar tools visible (Add Node, Pan, Zoom)

### 1.2 Dashboard Screenshot (Optional)

**Target:** Could be used for future sections or marketing

**Steps:**
1. Navigate to: `http://localhost:30080/dashboard`
2. Ensure there's some sample data (agents, runs)
3. Take screenshot: **1920x1080** or **2560x1440**
4. Save as: `apps/zerg/frontend-web/public/images/landing/dashboard-screenshot.png`

**Quality Checklist:**
- [ ] Shows agent cards or run history
- [ ] Demonstrates the "human stories, not dashboards" UX
- [ ] No personal/sensitive data visible

---

## Task 2: Integrate Screenshots

### 2.1 Update NerdSection

**File:** `apps/zerg/frontend-web/src/components/landing/NerdSection.tsx`

Find this line (around line 130):
```tsx
<AppScreenshotFrame title="Visual Workflow Builder">
  <img
    src="/images/landing/canvas-preview.png"  // ← Change this
    alt="Visual workflow canvas..."
  />
</AppScreenshotFrame>
```

Change to:
```tsx
<AppScreenshotFrame title="Visual Workflow Builder">
  <img
    src="/images/landing/canvas-screenshot.png"  // ← New screenshot
    alt="Visual workflow canvas showing AI agent nodes connected with triggers and actions"
  />
</AppScreenshotFrame>
```

### 2.2 Update Scenario Cards (Optional)

If you want to replace scenario illustrations with real screenshots:

**File:** `apps/zerg/frontend-web/src/components/landing/ScenariosSection.tsx`

The scenarios currently use illustrations. You could:
- Keep illustrations (they're polished and purpose-built)
- OR replace with real screenshots showing each use case

**Recommendation:** Keep illustrations for now - they communicate the concepts better than raw UI screenshots.

---

## Task 3: Demo Video (Future)

When ready to add a demo video:

### 3.1 Record the Video

**Content suggestions:**
- 60-90 seconds max
- Show: Creating a workflow from scratch
- Narration or captions explaining each step
- End with the workflow running successfully

**Technical specs:**
- Format: MP4 (H.264)
- Resolution: 1920x1080 or 1280x720
- File size: Under 20MB ideally

### 3.2 Integrate the Video

**File:** `apps/zerg/frontend-web/src/components/landing/DemoSection.tsx`

Find this (around line 15):
```tsx
<DemoVideoPlaceholder
  title="Product Demo"
  // videoUrl="/videos/swarmlet-demo.mp4"  // ← Uncomment when ready
/>
```

Uncomment and update:
```tsx
<DemoVideoPlaceholder
  title="Product Demo"
  videoUrl="/videos/swarmlet-demo.mp4"
/>
```

Save video to: `apps/zerg/frontend-web/public/videos/swarmlet-demo.mp4`

---

## Task 4: Validation

### Visual QA Checklist

Run `make dev` and check each section:

- [ ] **Hero**: Robot mascot animates, gradient text visible
- [ ] **PAS Section**: Text is accurate (no "messages" or "home" monitoring claims)
- [ ] **Scenarios**: Three cards render with images
- [ ] **Demo Section**: Shows video player frame with macOS chrome
- [ ] **Differentiation**: Comparison table renders
- [ ] **Nerd Section**:
  - [ ] "Latest AI models" says "OpenAI GPT-5.2, GPT-5 Mini" (NOT Claude)
  - [ ] Canvas screenshot frame has traffic light dots
  - [ ] Screenshot loads without errors
- [ ] **Integrations**: "SOON" badges on Calendar, Health, Home Assistant
- [ ] **FAQ**: "What LLM do you use?" answer mentions OpenAI only
- [ ] **Footer**: All links work (/pricing, /docs, /changelog, /privacy, /security)

### Browser Testing

Test in:
- [ ] Chrome (latest)
- [ ] Firefox (latest)
- [ ] Safari (if on Mac)
- [ ] Mobile viewport (responsive)

### Build Verification

```bash
cd apps/zerg/frontend-web
bun run build
```

Should complete with no errors.

---

## File Locations Reference

```
apps/zerg/frontend-web/
├── public/
│   ├── images/
│   │   └── landing/
│   │       ├── canvas-preview.png      # Current illustration
│   │       ├── canvas-screenshot.png   # NEW: Real screenshot
│   │       └── dashboard-screenshot.png # NEW: Optional
│   └── videos/
│       └── swarmlet-demo.mp4           # NEW: When ready
├── src/
│   ├── components/
│   │   └── landing/
│   │       ├── AppScreenshotFrame.tsx  # Screenshot wrapper
│   │       ├── DemoVideoPlaceholder.tsx # Video placeholder
│   │       ├── DemoSection.tsx         # Demo section
│   │       ├── NerdSection.tsx         # Update image path here
│   │       └── ScenariosSection.tsx    # Optional updates
│   └── pages/
│       └── LandingPage.tsx             # Main landing page
```

---

## Questions?

- Slack: #swarmlet-dev
- The original implementation PR: [link to PR if applicable]
- Design reference: The current illustrations set the visual tone - screenshots should match the dark theme aesthetic

---

## Definition of Done

- [ ] Canvas screenshot captured at proper resolution
- [ ] Screenshot integrated into NerdSection
- [ ] Visual QA checklist passed
- [ ] Build passes with no errors
- [ ] PR created with screenshot assets
