# Swarmlet Landing Page PRD

**Document Version:** 1.0
**Created:** December 2024
**Status:** Ready for Implementation

---

## Executive Summary

Swarmlet currently drops users directly into a Google Auth flow with zero context, value proposition, or demo access. This document defines the strategy, structure, and implementation plan for a high-converting landing page that follows modern SaaS best practices and positions Swarmlet as a **personal AI assistant** rather than an enterprise tool.

### Core Problem

- Users hit the app → Immediate auth wall → No understanding of value
- Zero demo capability, no social proof, no CTAs
- Missing conversion funnel from "curious visitor" to "signed-up user"

### Solution

Create a beautiful, conversion-optimized landing page at `/` that:

1. Clearly communicates value in 3 seconds
2. Uses one primary CTA ("Start Free") repeated throughout
3. Hooks "normal users" above the fold, rewards "nerds" below
4. Provides demo-able content without requiring authentication

---

## Brand & Positioning

### Identity

- **Product Name:** Swarmlet
- **Tagline:** "Your own super-Siri for email, health, chats, and home."
- **Logo:** Purple robot helmet with green visor eyes (already exists as `SwarmLogo.tsx`)

### Positioning Statement

> "A personal AI that connects to all your apps and quietly runs your life admin."

This positions us AGAINST:

- Enterprise agent platforms (Zapier, n8n, enterprise AI dashboards)
- Cold, complicated automation tools
- Per-seat pricing and "talk to sales" friction

### Two Audiences, One Page

| Audience               | Above the Fold                 | Below the Fold                     |
| ---------------------- | ------------------------------ | ---------------------------------- |
| **"Normal but busy"**  | Outcomes, feelings, simplicity | Social proof, trust signals        |
| **"Nerds / builders"** | Clear what it does             | Technical details, workflows, APIs |

**Strategy:** Hook normals with emotional outcomes, reward nerds with technical depth.

---

## Design System

### Existing Design Tokens (Use These)

```css
/* Colors */
--color-surface-page: #09090b; /* Background */
--color-surface-card: #27272a; /* Cards */
--color-surface-section: #18181b; /* Sections */
--color-brand-primary: #6366f1; /* Indigo - Primary CTA */
--color-brand-secondary: #818cf8; /* Lighter indigo - Hover */
--color-brand-accent: #f59e0b; /* Amber - Accent */
--color-text-primary: #fafafa; /* White text */
--color-text-secondary: #a1a1aa; /* Muted text */
--color-text-muted: #71717a; /* Very muted */
--color-intent-success: #10b981; /* Green */
--color-intent-error: #ef4444; /* Red */

/* Typography */
--font-family-display: "General Sans", sans-serif; /* Headlines */
--font-family-base: "Inter", sans-serif; /* Body */
--font-family-mono: "JetBrains Mono", monospace; /* Code */

/* Spacing */
--space-4: 16px;
--space-6: 24px;
--space-8: 32px;
--space-12: 48px;
--space-16: 64px;

/* Radius */
--radius-md: 6px;
--radius-lg: 8px;
--radius-xl: 12px;
--radius-2xl: 16px;

/* Shadows */
--shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.5);
--shadow-glow: 0 0 24px rgba(99, 102, 241, 0.25);
```

### Visual Direction

1. **Dark theme** - Consistent with app
2. **Subtle purple glow** - Behind hero, like branding pages
3. **Particle background** - Already exists in `particle.css`
4. **Card-based sections** - Using existing card styles
5. **Gradient text** - For headlines (indigo → purple)

### Existing Components to Reuse

- `SwarmLogo.tsx` - Animated SVG logo with glow effects
- `btn-primary`, `btn-secondary`, `btn-ghost` - Button styles
- `particle-bg` - Background effect
- Modal system - For potential video/demo modals

---

## Page Structure (AIDA + PAS Framework)

### 1. HERO SECTION (Attention + First Action)

**Goal:** 3-second clarity

```
┌─────────────────────────────────────────────────────────────┐
│                      [SwarmLogo]                            │
│                                                             │
│     "Your own super-Siri for email, health,                 │
│              chats, and home."                              │
│                                                             │
│   Link your health data, location, inboxes, chats,          │
│   and smart home into one brain. Get one place where        │
│   your AI sees everything and quietly handles the           │
│   annoying stuff.                                           │
│                                                             │
│        [Start Free]      [See it in action ↓]               │
│                                                             │
│              ┌─────────────────────────┐                    │
│              │   Hero Visual:          │                    │
│              │   Central AI orb with   │                    │
│              │   app icons connecting  │                    │
│              └─────────────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

**Copy Options:**

- **Headline A:** "Your own super-Siri for email, health, chats, and home." ← CHOSEN
- **Headline B:** "Connect all your apps. Let one AI handle the busywork."
- **Headline C:** "A personal AI that watches your life so you don't have to."

**CTAs:**

- Primary: `Start Free` → Triggers Google OAuth
- Secondary: `See it in action` → Smooth scroll to scenarios

---

### 2. PAS BLOCK (Problem → Agitate → Solution)

**Problem (3 bullets):**

- "**Digital Fragmentation.** Your health, calendar, and chats are scattered across a dozen apps."
- "**Notification Overload.** You miss what matters because everything is shouting at once."
- "**Complexity Fatigue.** Automation tools feel like wiring a server, not living your life."

**Agitate:**

> "Siri can't remember what you said five minutes ago. What if your assistant was actually… smart?"

**Solution:**

> "Swarmlet is a personal AI hub: plug in the tools you already use, and it watches your life signals — health, location, messages, home — to keep you organized automatically."

---

### 3. HOW IT WORKS (3 Human Scenarios)

**Card 1: Daily Health & Focus Check**

- Icon: 🩺
- Connect your watch/health app + calendar
- Each morning, your AI checks sleep & schedule
- If recovery is low, it suggests rescheduling heavy meetings
- `[Start Free]`

**Card 2: Inbox + Chat Guardian**

- Icon: 📬
- Connect email, Slack, Discord
- AI watches for "urgent", "manager", or "family" tags
- You get a single digest with the 5 things that actually matter
- `[Start Free]`

**Card 3: Smart Home That Knows Your Patterns**

- Icon: 🏠
- Use location + time + calendar
- "Leaving work?" → preheat/cool home, turn on lights, set playlist
- One trigger, multiple actions, zero config
- `[Start Free]`

---

### 4. DIFFERENTIATION TABLE

| Aspect    | **Swarmlet**                  | "Enterprise agent platforms"   |
| --------- | ----------------------------- | ------------------------------ |
| Built for | Individuals + nerds           | Teams, managers, enterprises   |
| Setup     | Connect your own apps in mins | IT tickets, SSO, sales calls   |
| Pricing   | Flat, cheap personal plan     | Per-seat, "talk to sales"      |
| UX        | Human stories, not dashboards | Admin panels, dashboards, CRMs |
| Control   | You own your data & agents    | Shared company workspace       |

---

### 5. NERD SECTION ("For People Who Like Knobs")

**Headline:** "For builders, hackers, and power users"

**Features (bullet grid):**

- 🔧 Build custom agents and workflows without fighting YAML
- 🔌 Connect anything with webhooks and APIs
- 🧠 Bring your own LLM keys (tune cost/latency)
- 📊 Inspect what your agents did, step-by-step logs
- ⚡ Run on schedule or triggered by events
- 🎨 Visual workflow canvas (drag-and-drop)

**Technical highlights:**

- LangGraph-powered agent execution
- Per-token LLM streaming over WebSocket
- MCP (Model Context Protocol) integration
- Two-tier credential management (account + agent level)

---

### 6. INTEGRATIONS SHOWCASE

**Headline:** "Works with the tools you already use"

**Grid of integration logos:**

| Notifications  | Project Management |
| -------------- | ------------------ |
| Slack          | GitHub             |
| Discord        | Jira               |
| Email (Resend) | Linear             |
| SMS (Twilio)   | Notion             |
| iMessage       |                    |

**Subtext:** "And any MCP-compatible server"

---

### 7. SOCIAL PROOF & TRUST

**Phase 1 (Launch):**

- "Loved by early users who hate dashboards"
- Integration logos (as "Works with" - not endorsements)
- "Is my data safe?" expandable FAQ

**Phase 2 (Post-launch):**

- Real testimonials with names/photos
- Usage stats ("10,000 automations run")
- Case study snippets

**Trust FAQ:**

- "How does authentication work?" → Google OAuth, JWT sessions
- "Where is my data stored?" → Encrypted at rest, PostgreSQL
- "Can I delete my data?" → Yes, full account deletion available

---

### 8. PRICING BLOCK

**Simple, single-tier for launch:**

```
┌─────────────────────────────────────────────────────────────┐
│                     Personal Plan                            │
│                                                             │
│                    $X / month                               │
│         (or Free tier with limited agents)                  │
│                                                             │
│  ✓ Unlimited automations                                    │
│  ✓ All integrations                                         │
│  ✓ Visual workflow builder                                  │
│  ✓ Real-time streaming                                      │
│  ✓ No sales calls                                           │
│  ✓ Cancel anytime                                           │
│                                                             │
│              [Start Free]                                   │
│                                                             │
│         "Start free, upgrade when it sticks"                │
└─────────────────────────────────────────────────────────────┘
```

**Note:** Pricing TBD - can use placeholder or hide section initially.

---

### 9. FOOTER CTA

**Final PAS reminder:**

> "Life is noisy. You deserve a brain that pays attention for you."

**CTA:** `[Start Free]`

**Secondary links:**

- Documentation
- Changelog
- Security
- "Talk to the founder" / Contact

---

## Technical Implementation

### Routing Changes

```typescript
// Current: All routes wrapped in AuthGuard
// New: Landing page exempt from auth

// src/routes/App.tsx changes:
const routes = useRoutes([
  {
    path: "/",
    element: <LandingPage />  // No AuthGuard
  },
  {
    path: "/dashboard",
    element: <AuthGuard><DashboardPage /></AuthGuard>
  },
  // ... rest of authenticated routes
]);
```

### New Files to Create

```
src/
├── pages/
│   └── LandingPage.tsx              # Main landing page component
├── components/
│   └── landing/
│       ├── HeroSection.tsx          # Hero with logo, headline, CTAs
│       ├── PASSection.tsx           # Problem-Agitate-Solution
│       ├── ScenariosSection.tsx     # 3 human scenario cards
│       ├── DifferentiationTable.tsx # Us vs Enterprise
│       ├── NerdSection.tsx          # Technical features
│       ├── IntegrationsGrid.tsx     # Integration logos
│       ├── TrustSection.tsx         # Social proof + FAQ
│       ├── PricingSection.tsx       # Pricing card
│       └── FooterCTA.tsx            # Final CTA + links
└── styles/
    └── landing.css                  # Landing page specific styles
```

### Key Implementation Details

1. **No AuthGuard** on landing page - visitors can browse freely
2. **"Start Free" CTA** → Triggers Google OAuth flow (existing `LoginOverlay`)
3. **Smooth scroll** for "See it in action" → Scrolls to scenarios section
4. **Reuse existing components** - SwarmLogo, buttons, design tokens
5. **Responsive design** - Mobile-first, existing breakpoints
6. **Performance** - Lazy load below-fold sections, optimize images

### Integration with Existing Auth

```typescript
// In LandingPage.tsx
import { useAuth } from "../lib/auth";
import { useNavigate } from "react-router-dom";

export function LandingPage() {
  const { isAuthenticated } = useAuth();
  const navigate = useNavigate();

  // If already logged in, redirect to dashboard
  useEffect(() => {
    if (isAuthenticated) {
      navigate("/dashboard");
    }
  }, [isAuthenticated, navigate]);

  const handleStartFree = () => {
    // Trigger the Google OAuth flow
    // Could show LoginOverlay or redirect to auth endpoint
  };

  return (
    <div className="landing-page">
      <HeroSection onStartFree={handleStartFree} />
      {/* ... */}
    </div>
  );
}
```

---

## Assets Needed

### Existing (Reuse)

- [x] SwarmLogo SVG component
- [x] Favicon and app icons
- [x] OG image (`og-image.png`)
- [x] Design tokens (colors, typography, spacing)
- [x] Button styles
- [x] Particle background

### To Create

- [ ] Hero visual (central AI orb with connected app icons)
- [ ] Scenario illustrations (or use emojis as placeholders)
- [ ] Integration logos (can use simple text/emoji initially)
- [ ] Screenshots/GIFs of actual app (optional for MVP)

---

## Implementation Phases

### Phase 1: MVP Landing (Priority)

**Goal:** Ship something better than auth wall

- [ ] Create `LandingPage.tsx` with basic structure
- [ ] Hero section with headline, subhead, CTAs
- [ ] PAS block with copy
- [ ] 3 scenario cards
- [ ] Single "Start Free" CTA flow
- [ ] Footer with links
- [ ] Routing changes (landing exempt from auth)
- [ ] Mobile responsive

**Estimate:** 1-2 days

### Phase 2: Polish

- [ ] Differentiation table
- [ ] Nerd section with technical details
- [ ] Integration logos grid
- [ ] Trust FAQ accordion
- [ ] Animations (scroll-triggered fade-ins)
- [ ] Hero visual (animated orb)

**Estimate:** 1 day

### Phase 3: Conversion Optimization

- [ ] Pricing section (when pricing is decided)
- [ ] A/B test headlines
- [ ] Add real testimonials
- [ ] Add demo video/GIFs
- [ ] Analytics tracking on CTAs

**Estimate:** Ongoing

---

## Success Metrics

### Primary

- **Conversion rate:** Visitors → Google Auth initiated
- **Sign-up completion:** Auth initiated → Account created
- **Time on page:** > 30 seconds (indicates engagement)

### Secondary

- **Scroll depth:** How far down the page users scroll
- **CTA click rate:** Which CTAs perform best
- **Bounce rate:** < 60% target

---

## Open Questions

1. **Pricing:** What's the actual pricing model?
   - Free tier limits?
   - Monthly price?
   - Annual discount?

2. **Demo approach:**
   - Static screenshots/GIFs? (fastest)
   - Video walkthrough? (medium effort)
   - Read-only demo mode? (most complex)

3. **Testimonials:** Do we have any early user quotes to use?

4. **Legal:** Do we need Terms of Service / Privacy Policy links?

---

## Appendix

### Competitor Research Notes

**What works in successful SaaS landing pages:**

- Clear value prop above the fold (< 3 seconds to understand)
- One primary CTA repeated throughout
- Social proof and trust signals
- Simple pricing (visible, not "contact sales")
- Problem-first, solution-second narrative

**What to avoid:**

- Jargon and clever metaphors that obscure meaning
- Multiple competing CTAs
- Enterprise-focused language for consumer products
- Hidden pricing
- No demo or screenshots

### Copy Bank (Alternative Headlines)

**Identity-focused:**

- "Your personal AI that never forgets"
- "Like having a second brain that actually works"
- "The AI assistant you wish Siri was"

**Outcome-focused:**

- "Stop managing apps. Start living."
- "One AI. All your tools. Zero chaos."
- "Finally, an automation that feels like magic"

**Problem-focused:**

- "Tired of 47 apps that don't talk to each other?"
- "Your digital life is fragmented. Fix it."
- "Because IFTTT wasn't built for humans"

---

## Changelog

| Date     | Version | Changes             |
| -------- | ------- | ------------------- |
| Dec 2024 | 1.0     | Initial PRD created |

---

_This document is the single source of truth for the Swarmlet landing page implementation. Reference this in all related development threads._
