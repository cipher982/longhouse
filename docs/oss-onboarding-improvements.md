# OSS Onboarding Improvements

**Goal:** Make Zerg go viral like Moltbot. Someone sees it on HN, tries it, finds value, stars/shares.

**Status:** Research phase

---

## The Problem (Current State)

### README Issues
- No clear value proposition in first 10 seconds
- Leads with architecture diagram and port numbers
- Internal jargon: "supervisor/worker", "commis", "Oikos", "BFF"
- No screenshot or demo
- No personality/brand identity
- Single pathway (developer-only)

### Install Friction
| Step | Current | Target |
|------|---------|--------|
| Clone | `git clone ...` | `curl ... \| bash` |
| Configure | Edit 247-line .env | Interactive wizard |
| Dependencies | Need OpenAI key upfront | Graceful degradation |
| Run | `make dev` | `zerg up` |
| Time to value | ~10 min if lucky | < 2 min |

### What We Actually Offer (Hidden Value)
1. **Sessions Timeline** — All your Claude/Codex/Gemini sessions unified and searchable
2. **Oikos Chat** — AI assistant with 65+ tools
3. **Background Agents** — Commis jobs run async while you work
4. **Shipper** — Real-time sync from laptop to Zerg ("magic moment")

None of this is visible in the current README.

---

## Competitive Analysis: Moltbot

**Why it went viral:** "The AI that actually does things"

### Landing Page Wins
- 7-word hook that explains value
- 50+ Twitter testimonials (social proof)
- One-liner install: `curl -fsSL https://molt.bot/install.sh | bash`
- Works through familiar apps (WhatsApp, Telegram)
- Concrete outcomes, not features

### README Wins
- Personality: "The lobster way" + memorable branding
- Visual first: WhatsApp screenshot showing real interaction
- Three pathways: Fastest / Guided / Developer
- Progressive disclosure: simple → complex
- Trust upfront: security, local-first, sandbox defaults

### Moltbot Install Flow (Pending Research)
<!-- Agent exploring this now - will fill in details -->

**Install script analysis:**
- [ ] What does `install.sh` actually do?
- [ ] How does it detect OS/platform?
- [ ] What gets installed (binary? npm package?)

**Onboarding wizard:**
- [ ] How do they collect API keys?
- [ ] Interactive prompts library used?
- [ ] Where are credentials stored?
- [ ] OAuth flow handling?

**First-run experience:**
- [ ] Time from install to "wow"
- [ ] What works without API keys?
- [ ] Graceful degradation strategy

---

## Zerg's Unfair Advantages

Things Moltbot doesn't have that we do:

1. **Session archive as product** — We're not just another chat assistant. The unified session timeline IS the product.
2. **StarCraft branding** — "Zerg" is memorable, has built-in community (gamers)
3. **Shipper magic** — "Your Claude Code session appears in Zerg before you switch tabs"
4. **Already built** — Sessions UI, shipper, ingest API all working

---

## Action Plan

### Phase 1: README Rewrite (Now)
- [ ] One-sentence hook: "All your AI coding sessions, unified and searchable"
- [ ] Screenshot of Sessions timeline
- [ ] Lean into StarCraft: "Spawn your swarm"
- [ ] Three pathways: Quick / Full / Developer
- [ ] Honest about current state + "coming soon"

### Phase 2: Reduce Install Friction (Next)
- [ ] Create `install.sh` that bootstraps everything
- [ ] Create `zerg onboard` interactive wizard
- [ ] Add root `docker-compose.yml` that just works
- [ ] Make UI work without API key (demo mode)

### Phase 3: One-Liner Install (Soon)
- [ ] `curl -fsSL https://swarmlet.com/install.sh | bash`
- [ ] Homebrew formula: `brew install zerg`
- [ ] npm/pip package for those who prefer

### Phase 4: Social Proof (After Launch)
- [ ] Get 5-10 real users
- [ ] Collect testimonials
- [ ] Add to landing page

---

## Patterns to Borrow

### From Moltbot
| Pattern | How We'd Adapt |
|---------|----------------|
| One-liner install | `curl -fsSL https://swarmlet.com/install.sh \| bash` |
| Interactive onboard | `zerg onboard` wizard for API keys, auth |
| Graceful degradation | Show UI even without keys, unlock features progressively |
| Multiple pathways | Quick start / Full setup / Developer |
| Personality | StarCraft theming: "spawn", "swarm", "hatchery" |

### Credential Handling (TBD from research)
<!-- Fill in after Moltbot exploration completes -->

---

## Open Questions

1. **Demo mode:** Can we show the Sessions UI with sample data, no API key needed?
2. **OAuth vs API keys:** Should we support Google/GitHub OAuth for LLM providers that offer it?
3. **Hosted quick-start:** Would `swarmlet.com/demo` (hosted instance) be faster than local install?
4. **Brand voice:** How much StarCraft theming is too much?

---

## Reference Links

- [VISION.md](/VISION.md) — Product vision and architecture
- [Moltbot GitHub](https://github.com/moltbot/moltbot) — Competitor analysis
- [Moltbot Landing](https://www.molt.bot/) — Landing page reference
- [Current README](/README.md) — What we're replacing

---

## Changelog

- **2026-01-29:** Initial doc created from HN/README analysis session
