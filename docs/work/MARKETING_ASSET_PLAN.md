# Swarmlet Marketing Asset Plan

**Date**: January 2, 2026
**Status**: Research & Planning
**Objective**: Transform the landing page from functional to conversion-optimized with professional assets

---

## Executive Summary

The Swarmlet landing page has a solid structure and good visual foundation, but lacks the polish and real content needed to convert visitors into users. The primary gaps are:

1. **Missing demo video** - The most critical conversion element
2. **Placeholder/generic imagery** - Scenario cards use AI-generated illustrations instead of real product screenshots
3. **No visual canvas workflow builder showcase** - One screenshot exists but it's tiny and not used effectively
4. **Empty "Watch This" section** - Just says "Demo Video Coming Soon"
5. **Weak social proof** - No testimonials, user counts, or trust indicators beyond security badges

**Current State**: 6/10 - Professional design, but feels like a prototype rather than a launched product.

---

## Section-by-Section Audit

### 1. Hero Section ✅ GOOD
**Current State**: Strong visual with animated logo, clear value prop, clean CTA

**What Works**:
- Compelling tagline: "Your own super-Siri for email, health, chats, and home"
- Clean design with animated particle effects
- Clear primary CTA: "Start Building" button

**What's Missing**:
- No social proof (e.g., "Join 1,000+ users automating their lives")
- No secondary validation (e.g., product badges, testimonials preview)

**Recommendation**: Keep as-is for now. Consider adding subtle trust indicator below CTA once you have users.

---

### 2. PAS (Problem-Agitate-Solution) Section ✅ GOOD
**Current State**: Well-written copy explaining the value prop

**What Works**:
- Clear problem statement: "What if your assistant was actually... smart?"
- Good description of how Swarmlet differs from basic automation

**What's Missing**: Nothing critical - this section is solid.

---

### 3. Scenarios Section ⚠️ NEEDS IMPROVEMENT
**Current State**: Three cards with AI-generated illustrations

**What Works**:
- Good copy for each scenario (Health & Focus Check, Inbox + Chat Guardian, Smart Home That Knows You)
- Nice visual style with glassmorphism cards
- "Start Free" CTAs on each card

**What's Missing**:
- **Real product screenshots instead of generic illustrations**
- The current images are beautiful but don't show the actual product
- Visitors can't see HOW it works, just conceptual representations

**Assets Needed**:
1. **Health scenario**: Screenshot of actual health agent dashboard showing WHOOP integration, morning digest, etc.
2. **Inbox scenario**: Screenshot of email analysis workflow or chat interface analyzing messages
3. **Smart home scenario**: Screenshot of home automation workflow with real integrations (Traccar, etc.)

**Priority**: HIGH - These need to be real product screenshots to build credibility

---

### 4. Demo Section 🚨 CRITICAL
**Current State**: Empty placeholder with "Demo Video Coming Soon"

**What Works**: Nothing - this section is completely non-functional

**What's Missing**: Everything

**Assets Needed**:
1. **3-5 minute product demo video** showing:
   - Quick overview of the platform
   - Building a workflow in the canvas
   - Chat interface interaction with Jarvis
   - Agent execution and results
   - Dashboard management

**Alternative Options** (if video production is too heavy):
1. **Animated GIF carousel**: 3-4 high-quality GIFs showing key workflows
2. **Interactive canvas preview**: Embedded mini-version of the canvas with a pre-built workflow
3. **Before/After screenshots**: Show the problem → Swarmlet workflow → solution

**Priority**: CRITICAL - This is the #1 conversion blocker. Most SaaS landing pages have a demo.

**Recommended Approach**: Start with a simple screen recording using:
- QuickTime/OBS to record 1920x1080 screen
- 3-minute walkthrough:
  - "Here's Swarmlet" (0:00-0:30)
  - "Build a workflow visually" (0:30-1:30)
  - "Chat with your agents" (1:30-2:30)
  - "Monitor everything" (2:30-3:00)
- Add background music and simple title cards
- Export to MP4, host on Cloudflare R2 or Mux

---

### 5. Differentiation Section ⚠️ NEEDS IMPROVEMENT
**Current State**: "Watch How It Works" section but no actual demo content (overlaps with section 4)

**Issue**: This section appears to duplicate the Demo section above. The screenshot shows the same empty demo placeholder.

**Recommendation**:
- Either merge with Demo Section OR
- Repurpose as "Why Swarmlet?" comparison table:
  - Swarmlet vs. Zapier/Make
  - Swarmlet vs. Custom scripts
  - Swarmlet vs. ChatGPT plugins

---

### 6. Nerd Section ✅ GOOD
**Current State**: "Built Different" comparison table

**What Works**:
- Clear differentiation: "Not another enterprise tool pretending to be personal"
- Good comparison points (individuals + nerds vs enterprise tools)
- Clean table format

**What's Missing**: Nothing critical - this section works well.

---

### 7. Integrations Section ⚠️ NEEDS ASSETS
**Current State**: "For People Who Like Knobs" with 6 feature cards + integration logos

**What Works**:
- Good feature descriptions (custom agents, connect anything, latest AI models, etc.)
- Clean icon-based cards
- Integration logo grid at bottom

**What's Missing**:
- **Visual workflow builder screenshot** - You have `canvas-preview.png` but it's not used effectively here
- The "Visual canvas" card should link to an actual screenshot or demo
- Consider showing a real workflow with multiple integrations connected

**Assets Needed**:
1. **Canvas screenshot with realistic workflow** (you already have this at `/images/landing/canvas-preview.png` but it needs better visibility)
2. **Integration grid screenshot** showing actual connected services in the settings page

**Priority**: MEDIUM - Nice to have, not critical for MVP

---

### 8. Trust Section ✅ GOOD
**Current State**: FAQ accordion + security badges

**What Works**:
- Clear FAQ addressing common concerns (auth, data storage, deletion, AI training)
- Security badges (encrypted credentials, HTTPS, full deletion, no training)
- Good copy

**What's Missing**:
- No testimonials or user quotes
- No user count or social proof ("Join 500 users" etc.)

**Assets Needed** (when you have users):
1. 3-5 testimonial quotes with names/photos
2. User count badge
3. Optional: logos of companies using Swarmlet (if applicable)

**Priority**: LOW - Fine for pre-launch, add social proof post-launch

---

### 9. Footer CTA Section ✅ GOOD
**Current State**: "Works With Your Tools" integration grid

**What Works**:
- Shows breadth of integrations (Slack, Discord, Email, SMS, GitHub, Jira, Linear, Notion, Google Calendar, Apple Health, MCP Servers)
- Clean grid layout
- Good closing statement: "And anything you build webhooks, REST APIs, or SSH"

**What's Missing**: Nothing critical

---

## Real Product Screenshots Audit

### Current Assets
Located at `/apps/zerg/frontend-web/public/images/landing/`:

1. **canvas-preview.png** (559KB) - Small workflow diagram, looks real but basic
2. **hero-orb.png** (~0.91MB) - Decorative asset, not product screenshot
3. **integrations-grid.png** (~1.06MB) - Generic/AI-generated, not real product
4. **og-image.png** (~0.85MB) - Social share image (note: current meta tag uses `/og-image.png` at repo root public)
5. **scenario-health.png** (~220KB) - AI-generated illustration (smartwatch)
6. **scenario-home.png** (~235KB) - AI-generated illustration (neon house)
7. **scenario-inbox.png** (~264KB) - AI-generated illustration (email windows)
8. **trust-shield.png** (~1.07MB) - Decorative asset

### Real App Screenshots Available

From the app audit, here's what's actually screenshot-worthy:

#### 1. Canvas Page (http://localhost:30080/canvas)
**Current State**: Clean workflow builder with node graph

**Good for**:
- Hero visual or demo section
- "Visual canvas" feature showcase
- Tutorial/walkthrough content

**Screenshot Quality**: Professional, ready to use

**Recommended Uses**:
- Replace `canvas-preview.png` with full-resolution canvas screenshot
- Create 3-4 progressive screenshots showing workflow building (empty canvas → nodes → connections → execution)

#### 2. Chat Page (http://localhost:30080/chat)
**Current State**: Clean empty state "SYSTEM READY" with conversation sidebar

**Good for**:
- Chat interface showcase
- Before/after demo (empty → conversation with results)

**Screenshot Quality**: Professional, but empty state

**Recommended Uses**:
- Screenshot with actual conversation thread showing agent delegation
- Show reasoning tokens badge, tool use indicators

#### 3. Dashboard (http://localhost:30080/dashboard)
**Current State**: Agent list with status badges, usage stats, action buttons

**Good for**:
- Management/monitoring showcase
- "Your agents at a glance" feature
- Success state after workflow creation

**Screenshot Quality**: Professional, real data visible

**Recommended Uses**:
- Hero alternative (shows "you'll manage agents like this")
- Trust section ("see all your agents' activity")

#### 4. Integrations Page (http://localhost:30080/settings/integrations)
**Current State**: Clean settings page with Slack/Discord/Email/SMS integration cards

**Good for**:
- "Connect anything" proof
- Settings/configuration showcase

**Screenshot Quality**: Professional but mostly empty state

**Recommended Uses**:
- Replace generic integration images with real settings screenshot
- Show "configured" state if possible

---

## Asset Creation Priority Matrix

| Asset | Impact | Effort | Priority | ETA |
|-------|--------|--------|----------|-----|
| **Demo video (3-5 min)** | 🔥 Critical | High | P0 | 1-2 days |
| **Canvas workflow screenshots** | High | Low | P1 | 2 hours |
| **Chat conversation screenshots** | High | Medium | P1 | 1 hour |
| **Dashboard with real data** | Medium | Low | P2 | 30 min |
| **Scenario card replacements** | High | Medium | P1 | 3 hours |
| **Integration screenshots** | Low | Low | P3 | 1 hour |
| **Testimonials (post-launch)** | High | N/A | P4 | After users |

---

## Detailed Asset Production Plan

### Phase 1: Quick Wins (1 day)
**Goal**: Replace placeholder images with real product screenshots

#### 1.1 Canvas Screenshots
**Time**: 2 hours
**Steps**:
1. Open canvas, create a realistic workflow (e.g., "Morning Email Digest")
2. Take 4 progressive screenshots:
   - Empty canvas with sidebar visible
   - First agent node added
   - Multiple nodes connected
   - Full workflow with 5-6 nodes
3. Export at 2x resolution (2400px wide)
4. Optimize with ImageOptim or similar
5. Replace `canvas-preview.png` and add to scenario cards

#### 1.2 Chat Conversation Screenshots
**Time**: 1 hour
**Steps**:
1. Start new conversation in chat
2. Ask a realistic question: "Analyze my morning emails and summarize urgent items"
3. Let it spawn workers/show tool use
4. Take screenshots at key moments:
   - User message sent
   - Agent thinking/delegating
   - Tool execution visible
   - Final response with results
5. Use for scenario cards or demo section

#### 1.3 Dashboard Screenshot
**Time**: 30 minutes
**Steps**:
1. Ensure dashboard has 5-6 agents with varied statuses
2. Take clean screenshot showing full dashboard view
3. Consider using as hero alternative or trust section visual

### Phase 2: Demo Content (2 days)
**Goal**: Create primary conversion asset - the demo video

#### 2.1 Script & Storyboard
**Time**: 2 hours
**Sections**:
```
[0:00-0:15] Hook
"Tired of copying data between tools? Watch how Swarmlet
lets you build AI workflows that actually work."

[0:15-0:45] Overview
- Show dashboard
- "Create agents that monitor your email, health data,
  smart home - anything you connect"

[0:45-1:45] Canvas Workflow Building
- Start from empty canvas
- "Let's build a morning digest agent"
- Drag Email Watcher node
- Connect to Content Analyzer
- Connect to Slack Notifier
- "No coding required"

[1:45-2:30] Chat Interface
- Switch to chat
- "Or just ask Jarvis"
- Show natural language interaction
- Agent executes workflow
- Results appear in real-time

[2:30-3:00] Wrap-up
- Show dashboard with multiple agents running
- "Your personal AI swarm, working for you 24/7"
- CTA: "Start building free at swarmlet.com"
```

#### 2.2 Recording
**Time**: 4 hours (including retakes)
**Tools**:
- Screen recording: OBS Studio or QuickTime (Mac)
- Audio: Shure MV7 or Rode NT-USB (if you have) or MacBook mic
- Resolution: 1920x1080 @ 30fps
- Cursor: Visible but not distracting

**Tips**:
- Record in 1-2 minute segments, easier to edit
- Use `?log=minimal` to reduce console noise
- Seed some realistic data first (morning emails, health stats, etc.)
- Practice once before recording

#### 2.3 Editing
**Time**: 4 hours
**Tools**:
- DaVinci Resolve (free) or iMovie (Mac)
- Background music: Epidemic Sound, Artlist, or free music from YouTube Audio Library

**Edit Steps**:
1. Import all clips
2. Cut out dead air, mistakes, slow parts
3. Add title cards at section transitions
4. Add subtle zoom-ins on key moments (node connections, results appearing)
5. Add background music (keep it subtle - 20-30% volume)
6. Color correction if needed
7. Export: H.264, 1920x1080, 5-10 Mbps

#### 2.4 Hosting & Embedding
**Options**:
1. **Self-hosted** (Recommended for control):
   - Upload to Cloudflare R2
   - Use video.js or Plyr for player
   - Fallback poster image

2. **Mux** (Recommended for easy embed):
   - Upload to Mux
   - Get embed code
   - Built-in adaptive streaming

3. **YouTube** (Easiest but less control):
   - Upload to YouTube
   - Embed with `youtube-nocookie.com`
   - Add to landing page

**Landing Page Integration**:
```tsx
// In DemoVideoPlaceholder.tsx
<video
  src="/videos/swarmlet-demo.mp4"
  poster="/images/landing/demo-thumbnail.jpg"
  controls
  width="100%"
/>
```

### Phase 3: Scenario Card Replacements (4 hours)
**Goal**: Replace AI-generated illustrations with real product screenshots

#### 3.1 Health Scenario
**Screenshot Needed**: Morning health digest showing real data

**Steps**:
1. Seed WHOOP data (or use mock data)
2. Create "Morning Health Check" agent
3. Run it and screenshot the results
4. Show: recovery score, sleep quality, suggested actions
5. Alternative: Screenshot of health agent configuration showing WHOOP integration

**Where to shoot**:
- Dashboard showing health agent with recent run
- Chat showing health digest message
- Canvas showing health workflow

#### 3.2 Inbox Scenario
**Screenshot Needed**: Email analysis results or inbox workflow

**Steps**:
1. Create "Inbox Guardian" agent
2. Connect to email (or mock some emails)
3. Show analysis: priority routing, spam detection, auto-replies
4. Screenshot the workflow or results

**Where to shoot**:
- Canvas with email workflow visible
- Chat showing email summary
- Dashboard showing inbox agent with stats

#### 3.3 Smart Home Scenario
**Screenshot Needed**: Home automation workflow

**Steps**:
1. Create "Smart Home Manager" agent
2. Show Traccar integration for location
3. Connect to home devices (or show the workflow even if not fully functional)
4. Screenshot the visual workflow

**Where to shoot**:
- Canvas with home automation workflow (Traccar → IF conditions → device controls)
- Dashboard showing home agent activity

### Phase 4: Polish & Optimization (2 hours)
**Goal**: Final touches before considering it "done"

#### 4.1 Image Optimization
- Run all screenshots through ImageOptim
- Target: < 500KB per image
- Ensure 2x retina resolution (double size, then optimize)

#### 4.2 Metadata & SEO
- Update `og-image.png` to feature real product screenshot
- Add alt text to all images in landing page components
- Ensure video has proper thumbnail

#### 4.3 Performance
- Lazy load images below fold
- Add `loading="lazy"` to image tags
- Consider WebP format for screenshots (with PNG fallback)

---

## Alternative: Minimum Viable Demo (If Time-Constrained)

If a full video is too heavy, here's a faster path:

### Option A: Animated GIF Showcase (4 hours total)
**Instead of video, create 3-4 high-quality GIFs**:

1. **Canvas workflow building** (15 seconds)
   - Screen recording of building a workflow
   - Export as GIF with Gifox or Kap
   - Loop infinitely

2. **Chat interaction** (10 seconds)
   - User asks question
   - Agent responds with results
   - Loop

3. **Dashboard monitoring** (10 seconds)
   - Agents running
   - Status updates
   - Stats changing
   - Loop

**Pros**:
- Faster to create than full video
- Auto-play on landing page (no click required)
- Lower bandwidth

**Cons**:
- No audio explanation
- Shorter, less detail

### Option B: Interactive Canvas Demo (8 hours)
**Embed a read-only canvas instance with a pre-built workflow**

**Steps**:
1. Create "Example Workflow" that's always available
2. Embed canvas iframe in demo section
3. Add subtle animation showing execution flow
4. Make it interactive (pan/zoom but read-only)

**Pros**:
- Most engaging option
- Shows real product
- Visitors can explore

**Cons**:
- Most complex to build
- Performance concerns
- Requires backend support

---

## Post-Launch Assets (Wait Until You Have Users)

### Social Proof Package
**Collect after 50-100 users**:

1. **Testimonials**
   - 5-10 user quotes about specific value gained
   - Photos/avatars if users consent
   - Format: "Swarmlet saved me 5 hours/week on email management" - Name, Role

2. **Case Studies**
   - 2-3 detailed user stories
   - Problem → Solution → Results format
   - Include metrics if possible

3. **User Count Badge**
   - "Join 500+ users automating their workflows"
   - Update dynamically if possible

4. **Integration Proof**
   - "Connects to 50+ services" (count your integrations)
   - Logos of popular integrations
   - User-submitted workflow examples

### Launch Video (Optional)
**After initial traction, create polished launch video**:
- Hire professional voiceover
- Motion graphics for explainer sections
- User testimonials on video
- Multiple cuts for different channels (YouTube, Twitter, landing page)

---

## Budget Considerations (If Outsourcing)

If you want to outsource any of this:

| Asset | DIY Time | Outsource Cost | Quality Gain |
|-------|----------|----------------|--------------|
| Screenshots | 4 hours | $200-500 | Minimal - DIY is fine |
| Demo video (DIY quality) | 10 hours | $1,000-2,000 | Medium - DIY is 80% as good |
| Demo video (pro quality) | N/A | $3,000-8,000 | High - but overkill for MVP |
| Scenario illustrations (custom) | N/A | $500-1,500 | Medium - real screenshots better anyway |
| Landing page copy audit | N/A | $500-1,000 | Low - your copy is already good |

**Recommendation**: Do everything DIY for MVP launch. Outsource demo video polish after product-market fit.

---

## Success Metrics

How to know if your asset improvements worked:

### Before (Current Baseline)
- Bounce rate: ??% (measure this first)
- Time on page: ??s
- Scroll depth: ??%
- CTA click rate: ??%

### After (Target Improvements)
- Bounce rate: -15% (more people stay to watch demo)
- Time on page: +60s (watch demo video)
- Scroll depth: +20% (more people scroll to scenarios)
- CTA click rate: +25% (better understanding = more conversions)

### How to Measure
- Add Umami events for:
  - Video play start
  - Video completion %
  - Scenario card clicks
  - CTA button clicks
  - Scroll depth milestones

---

## Timeline Summary

### Week 1: Foundation (Current → Phase 1)
- Day 1-2: Real product screenshots
- Day 3: Scenario card replacements
- Day 4: Polish & test

### Week 2: Demo Video (Phase 2)
- Day 1: Script & practice
- Day 2: Record & first edit
- Day 3: Polish, music, export
- Day 4: Embed & optimize

### Week 3+: Post-Launch Iteration
- Measure analytics
- Collect user feedback
- Add social proof as it comes
- Iterate based on conversion data

---

## Quick Start: What To Do RIGHT NOW

If you have 2 hours today, do this:

1. **Seed realistic data** (30 min)
   - Create 3-4 example agents
   - Add realistic names, descriptions
   - Run a few sample workflows

2. **Take 10 screenshots** (60 min)
   - 3x canvas (empty, partial, full workflow)
   - 3x chat (before, during, after)
   - 2x dashboard (overview, detail)
   - 2x integrations (settings, configured)

3. **Replace scenario images** (30 min)
   - Update `scenario-health.png` with real screenshot
   - Update `scenario-inbox.png` with real screenshot
   - Update `scenario-home.png` with real screenshot
   - Push to production

**Expected improvement**: +10-15% conversion from this alone.

---

## Next Steps

1. **Prioritize**: Pick Phase 1 (quick wins) OR Phase 2 (demo video)
2. **Block time**: Schedule 1-2 dedicated days for asset creation
3. **Measure baseline**: Add analytics before making changes
4. **Ship incrementally**: Don't wait for perfection - ship screenshots first, demo later
5. **Iterate**: Measure, learn, improve

---

## Appendix: Asset Checklist

### Must Have (P0)
- [ ] Demo video OR animated GIF showcase
- [ ] Real canvas workflow screenshots (3-4)
- [ ] Real chat conversation screenshots (2-3)
- [ ] Replace all scenario card images with real screenshots

### Should Have (P1)
- [ ] Dashboard screenshot with real data
- [ ] Integration settings screenshot
- [ ] Optimized images (< 500KB each)
- [ ] Video thumbnail for demo section

### Nice to Have (P2)
- [ ] Before/after comparison visuals
- [ ] Interactive canvas demo (advanced)
- [ ] User testimonials (post-launch)
- [ ] Case study content (post-launch)

### Track Later (P3)
- [ ] Professional demo video (outsourced)
- [ ] Motion graphics explainer
- [ ] User count badge
- [ ] Logo grid of user companies

---

**Document Status**: Complete - Ready for Review
**Last Updated**: January 2, 2026
**Next Action**: Prioritize Phase 1 (screenshots) OR Phase 2 (video)
