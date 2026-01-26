# Swarm Map: RTS-Style Command Center UI

**Status:** 🟡 Spec Complete, Implementation Not Started
**Owner:** TBD
**Created:** 2026-01-25
**Last Updated:** 2026-01-25
**Origin Session:** Codex f9ab0a7e-582f-4c94-91e9-c8db9c724f4f (Jan 24-25, 2026)

---

## Executive Summary

Transform Zerg from a chat-first interface into a visual command center where users can monitor, dispatch, and manage autonomous AI agents like units in an RTS game. The core insight: **RTS UIs are designed for many units, limited attention, time-sensitive decisions** — exactly the workload of managing multiple AI agents.

### The Problem

| Level | Current State | Pain Point |
|-------|---------------|------------|
| L0 | Terminal tabs with Claude Code | Manual switching, no overview |
| L1 | Jarvis chat + hatch runners | Stuck in single chat pane, no spatial awareness |
| L2 | **This spec** | Walk around, visualize, dispatch without chat |

### Core Insight

The problem isn't "chat UI" — it's **situational awareness across many autonomous processes** + **low-friction context switching**.

---

## Vision Statement

> "Think a top down RTS style game where workers are buildings or nodes, or workers at a desk. I can walk around. Each room is maybe a repo..."

Users should be able to:
1. **See everything at a glance** — what's running, blocked, failed, waiting
2. **Click to dispatch** — not type to request
3. **Spatial memory** — remember where things are
4. **Drop in/out** — peek first, attach second

---

## Three-Surface Architecture

The complete vision has three interconnected views. **Surface 1 is the missing piece.**

### Surface 1: Map/World View (❌ NOT BUILT)

**The RTS map layer.** Spatial visualization of your agent swarm.

```
┌─────────────────────────────────────────────────────────────────┐
│                         SWARM MAP                                │
│  ┌───────────────┐    ┌───────────────┐    ┌───────────────┐    │
│  │   zerg repo   │    │  life-hub     │    │   hdr repo    │    │
│  │  ┌───┐ ┌───┐  │    │    ┌───┐      │    │               │    │
│  │  │🟢│ │🟡│  │    │    │🔴│      │    │    (idle)      │    │
│  │  └───┘ └───┘  │    │    └───┘      │    │               │    │
│  │   2 active    │    │  1 hard stop  │    │               │    │
│  └───────────────┘    └───────────────┘    └───────────────┘    │
│                                                                  │
│  Legend: 🟢 Running  🟡 Waiting  🔴 Hard Stop  ⚪ Idle           │
└─────────────────────────────────────────────────────────────────┘
```

**Components:**
- **Rooms** = Repos/domains/projects
- **Buildings** = Worker types / persistent services
- **Units** = Active runs / tasks (move, pulse, change color)
- **Fog of war** = Unknown state vs known state
- **Ambient cues** = Color, animation, size signal health

**Interactions:**
- Click unit → opens detail panel (Surface 3)
- Click room → filters triage list to that room
- Drag to pan, scroll to zoom
- Keyboard shortcuts for quick navigation

### Surface 2: Command List/Triage View (✅ BUILT — `/swarm`)

**The non-spatial triage interface.** Sort by urgency, quick actions.

Current implementation: `SwarmOpsPage.tsx`

- Runs sorted by attention level (hard → needs → soft → auto)
- Filter buttons: Attention / Active / Completed / All
- Summary cards: Hard stops, Needs you, Active, Total
- Detail panel with events and "Open thread" action

**What's missing from Surface 2:**
- [ ] Quick actions (ack, pause, kill, handoff)
- [ ] Explicit attention flags (L0-L3 badges)
- [ ] Bulk operations (select multiple, batch ack)
- [ ] Keyboard navigation (j/k to move, enter to open)

### Surface 3: Drop-In View (✅ PARTIAL)

**The detail/attach interface.** Deep inspection of a single run.

Current: Detail panel in Swarm Ops shows events, summary, "Open thread" button.

**Full vision includes:**
- **Peek mode** — read-only timeline of events/diffs
- **Attach mode** — take over the conversation, send messages
- **Handoff mode** — give back to automation with instructions
- **Overseer mode** — auto-approve L0/L1, alert on L2/L3

---

## Alert Scale System

Multi-level alerting so obvious prompts auto-continue while broken states hard-stop.

| Level | Action | Example | Visual |
|-------|--------|---------|--------|
| L0 | Auto-continue | "Did step 1, proceed?" | No indicator (silent) |
| L1 | Soft confirm | Trivial choice | Toast + one-click ack |
| L2 | Needs attention | Ambiguous, has tradeoffs | Yellow badge, sound |
| L3 | Hard stop | "I broke X / data loss" | Red badge, interrupt |

**Classification factors:**
- **Risk** — what's the blast radius?
- **Reversibility** — can we undo?
- **Certainty** — how confident is the agent?
- **Novelty** — has user seen this pattern before?

**Current state:** Swarm Ops has `classifyAttention()` with hard/needs/soft/auto based on regex patterns in signal text. Needs refinement to match L0-L3 spec.

---

## Technical Architecture

### Data Model

**Existing models (no changes needed):**
```python
# AgentRun — the "unit" in RTS terms
class AgentRun:
    id: int
    agent_id: int
    thread_id: int
    status: RunStatus  # queued, running, waiting, success, failed, cancelled
    summary: str | None
    signal: str | None
    signal_source: str | None
    error: str | None
    # ... timestamps, continuation_of_run_id, etc.

# RunEvent — the activity feed
class RunEvent:
    id: int
    run_id: int
    event_type: str
    payload: dict
    created_at: datetime
```

**New model for spatial layout:**
```python
class SwarmMapLayout(Base):
    """User's spatial arrangement of repos/rooms on the map."""
    __tablename__ = "swarm_map_layouts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # JSON structure:
    # {
    #   "rooms": [
    #     {"id": "zerg", "label": "Zerg", "x": 100, "y": 50, "width": 200, "height": 150},
    #     {"id": "life-hub", "label": "Life Hub", "x": 350, "y": 50, ...}
    #   ],
    #   "zoom": 1.0,
    #   "pan": {"x": 0, "y": 0}
    # }
    layout_json = Column(JSON, nullable=False, default={})

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
```

**Mapping runs to rooms:**

Option A: Use `AgentRun.agent_id` → `Agent.workspace` (if set)
Option B: Add `repo` field to AgentRun
Option C: Infer from run events (tool calls mention file paths)

**Recommendation:** Option A with fallback to "unassigned" room.

### API Endpoints

**New endpoints for Swarm Map:**

```
GET  /api/swarm/map-layout
     Returns user's saved layout or default

POST /api/swarm/map-layout
     Saves user's layout (room positions, zoom, pan)

GET  /api/swarm/units
     Returns all active runs grouped by room
     Response: { "rooms": { "zerg": [run1, run2], "life-hub": [run3] } }

WS   /api/swarm/live
     WebSocket for real-time unit updates
     Events: unit_spawned, unit_moved, unit_status_changed, unit_completed
```

**Existing endpoints (already used by Swarm Ops):**
```
GET  /api/jarvis/runs?limit=N
GET  /api/jarvis/runs/{id}/events
```

### Frontend Architecture

**New components:**

```
apps/zerg/frontend-web/src/
├── pages/
│   └── SwarmMapPage.tsx          # Main map view
├── components/
│   └── swarm-map/
│       ├── MapCanvas.tsx          # 2D canvas rendering
│       ├── Room.tsx               # Room/repo container
│       ├── Unit.tsx               # Individual run unit
│       ├── Minimap.tsx            # Overview minimap
│       ├── MapControls.tsx        # Zoom, pan, filter controls
│       └── UnitDetailPanel.tsx    # Side panel for selected unit
├── hooks/
│   └── useSwarmMapState.ts        # Map state management
└── styles/
    └── swarm-map.css
```

**Rendering approach:**

Option A: **HTML/CSS with transforms** — simpler, better a11y, limited performance
Option B: **Canvas 2D** — better performance for many units, harder a11y
Option C: **React Flow / xyflow** — existing canvas library, already used

**Recommendation:** Start with React Flow since it's already in the codebase for the workflow canvas. Units can be custom nodes.

### Real-Time Updates

**Current:** Swarm Ops polls every 5 seconds via React Query `refetchInterval`.

**For map:** WebSocket preferred for smooth unit movement/status changes.

```typescript
// useSwarmMapLive.ts
const useSwarmMapLive = () => {
  useEffect(() => {
    const ws = new WebSocket(`${WS_BASE}/swarm/live`);
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      switch (data.type) {
        case 'unit_spawned':
          addUnit(data.unit);
          break;
        case 'unit_status_changed':
          updateUnit(data.unit_id, data.status);
          break;
        case 'unit_completed':
          removeUnit(data.unit_id);
          break;
      }
    };
    return () => ws.close();
  }, []);
};
```

---

## Implementation Plan

### Phase 0: Foundation (Pre-req)
**Goal:** Ensure data layer supports spatial grouping.

- [ ] Add `workspace` or `repo` field to Agent model (if not present)
- [ ] Ensure runs can be grouped by repo/workspace
- [ ] Add SwarmMapLayout model + migration
- [ ] Add `/api/swarm/map-layout` endpoints

**Estimated effort:** 2-4 hours

### Phase 1: Static Map MVP
**Goal:** Render rooms and units, no interactivity beyond click-to-select.

- [ ] Create `SwarmMapPage.tsx` with React Flow canvas
- [ ] Implement room nodes (rectangular containers)
- [ ] Implement unit nodes (small circles/squares inside rooms)
- [ ] Color-code units by status (green=running, yellow=waiting, red=failed)
- [ ] Click unit → show detail panel (reuse from Swarm Ops)
- [ ] Add route `/map` to app router

**Estimated effort:** 1 day

### Phase 2: Persistence + Polish
**Goal:** Save layout, smooth UX.

- [ ] Save/load room positions from backend
- [ ] Drag rooms to rearrange
- [ ] Zoom/pan controls
- [ ] Minimap for navigation
- [ ] Keyboard shortcuts (arrow keys to pan, +/- to zoom)
- [ ] Mobile-responsive (or mobile-disabled with message)

**Estimated effort:** 1 day

### Phase 3: Live Updates
**Goal:** Units move/pulse in real-time.

- [ ] WebSocket endpoint `/api/swarm/live`
- [ ] Unit spawn animation (fade in)
- [ ] Status change animation (color pulse)
- [ ] Unit completion animation (fade out or move to "done" area)
- [ ] Sound cues for L2/L3 alerts (optional, user preference)

**Estimated effort:** 1 day

### Phase 4: Actions from Map
**Goal:** Dispatch and control from the map.

- [ ] Right-click unit → context menu (pause, kill, open thread)
- [ ] "New task" button per room → opens dispatch modal
- [ ] Bulk select units → batch actions
- [ ] Quick ack button for L1 alerts (inline on unit)

**Estimated effort:** 1 day

### Phase 5: Drop-In Modes
**Goal:** Full attach/handoff/overseer workflow.

- [ ] Peek mode: read-only timeline in side panel
- [ ] Attach mode: chat input appears, can send messages
- [ ] Handoff mode: "Resume automation with: [instructions]"
- [ ] Overseer mode: toggle auto-approve for L0/L1

**Estimated effort:** 2 days

### Phase 6: Alert Refinement
**Goal:** Intelligent L0-L3 classification.

- [ ] Backend: ML-based or rule-based classifier for alert levels
- [ ] Frontend: L0 auto-continues (no UI), L1 toast, L2 badge, L3 interrupt
- [ ] User preferences: sensitivity slider, sound on/off
- [ ] Alert history / audit log

**Estimated effort:** 2 days

---

## Success Metrics

### Quantitative
- **Context switch time:** < 2 seconds from "I wonder what X is doing" to seeing its status
- **Alert response time:** L3 alerts addressed within 1 minute
- **Dispatch time:** < 5 seconds to assign a new task (vs typing full chat message)
- **Coverage:** 90% of runs visible on map without scrolling (at default zoom)

### Qualitative
- User says "I can see everything at once"
- User navigates by spatial memory ("the life-hub room is on the right")
- User rarely needs to open chat to understand run status
- User feels "in control" of the swarm, not "waiting on Jarvis"

---

## Open Questions

1. **Room auto-discovery:** Should rooms be auto-created based on agent workspaces, or manually defined by user?
   - **Recommendation:** Auto-create with manual override

2. **Unit density:** What if 50 runs in one room? Cluster? Paginate?
   - **Recommendation:** Stack/cluster with count badge, expand on click

3. **Historical runs:** Show completed runs or only active?
   - **Recommendation:** Toggle "show completed" with time decay (fade out after 1h)

4. **Multi-user:** If team feature added, do users share a map or have individual views?
   - **Recommendation:** Individual views (like RTS player POV)

5. **Mobile:** Is this desktop-only or attempt responsive?
   - **Recommendation:** Desktop-first, mobile shows simplified list view (fall back to Swarm Ops)

---

## References

- **Original vision session:** Codex f9ab0a7e-582f-4c94-91e9-c8db9c724f4f
- **Swarm Ops implementation:** `apps/zerg/frontend-web/src/pages/SwarmOpsPage.tsx`
- **Workflow canvas (React Flow):** `apps/zerg/frontend-web/src/pages/canvas/`
- **Human PA model:** `~/git/obsidian_vault/AI-Sessions/2026-01-24-jarvis-worker-ux-design.md`
- **Visual workflow PRD:** `docs/completed/visual_workflow_canvas_prd.md`

---

## Progress Tracker

### Completed
- [x] Original vision session with Codex (Jan 24-25)
- [x] Swarm Ops triage page built (`/swarm`)
- [x] Signal extraction and attention classification
- [x] Scenario seeding for demos
- [x] This spec document

### In Progress
- [ ] (Nothing currently in progress)

### Blocked
- [ ] (Nothing blocked)

### Next Action
**Phase 0: Foundation** — Add workspace/repo grouping to data model, create SwarmMapLayout table.

---

## Appendix: RTS UI Patterns Reference

### StarCraft II Control Scheme
- **Minimap:** Always visible, click to jump
- **Selection panel:** Shows selected units, their health/state
- **Control groups:** Ctrl+# to assign, # to select
- **Alerts:** Audio + visual ping for attacks/events

### Factorio Production UI
- **Spatial layout:** Factory floor is the workspace
- **Tooltips:** Hover for production stats
- **Alerts:** Flashing icons for problems
- **Logistics view:** Overlay showing flow

### Relevant Patterns for Swarm Map
1. **Minimap** — essential for large swarms
2. **Selection groups** — "all failed runs", "all in zerg repo"
3. **Status overlays** — toggle to see only problems
4. **Ambient audio** — optional but effective for attention

---

*This spec is the canonical reference for the Swarm Map feature. Update this document as implementation progresses.*
