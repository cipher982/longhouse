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

### Moltbot Install Flow (Deep Dive)

**Install script (`https://molt.bot/install.sh`):**
- Detects OS via `$OSTYPE` (darwin* → macOS, linux-gnu* → Linux)
- Only installs missing deps (checks with `which`)
- Installs: Node.js 22+, Git, pnpm (via Corepack)
- npm package: `npm install -g moltbot --save-exact`
- Silent operations, colorized output, auto-cleanup

**Onboarding wizard (`moltbot onboard`):**
- Uses `@clack/prompts` for interactive TUI
- **Two flows:** QuickStart (defaults everything) vs Manual (full control)
- Risk acknowledgement UP FRONT (explicit --accept-risk for CI)
- Credential storage: `~/.clawdbot/agents/<id>/auth-profiles.json`
- Profile-based: supports multiple accounts per provider
- OAuth flow: browser opens → user pastes code → PKCE exchange

**First-run experience (~2-3 min total):**
- Install: 30-60s (downloads Node if missing)
- Onboard QuickStart: 60-90s
- Gateway startup: 5-10s
- First "hatch": 2-5s
- **Killer UX:** First interaction framed as "awakening" agent ("Wake up, my friend!")

**Graceful degradation:**
- If OAuth fails → fallback to API key paste
- If daemon fails → manual run option
- If channel setup fails → skipped, add later
- Gateway starts in "degraded mode" if no auth (can't call LLMs but UI works)

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

### Credential Handling (from Moltbot)

**Storage pattern:**
```
~/.clawdbot/agents/<agent-id>/auth-profiles.json
{
  "profiles": {
    "anthropic:user@example.com": { "type": "oauth", ... },
    "openai:default": { "type": "api_key", "key": "sk-..." }
  },
  "order": ["anthropic:user@example.com", "openai:default"],
  "lastGood": "anthropic:user@example.com"
}
```

**Key patterns:**
- Profile ID = `provider:identifier` (email for OAuth, "default" for API keys)
- Credentials SEPARATE from config (different file, different permissions)
- `lastGood` tracks which profile worked last (fast retry path)
- `order` array for user-controlled priority
- Background refresh for OAuth tokens before expiry
- Cooldown tracking for failed profiles (back off before retry)

**For Zerg:** Could adopt this for `~/.zerg/credentials/` instead of flat env vars

### UX Decisions That Reduce Friction

| Decision | Impact |
|----------|--------|
| **Defaults first** | QuickStart pre-chooses everything sensible |
| **Risk up front** | Security warning at start, not buried |
| **Hatch metaphor** | Frame first interaction as "awakening," not "configuring" |
| **Profile-based auth** | One flow supports multiple API keys per provider |
| **Modular channels** | Chat channels skippable; add later |
| **Token auto-gen** | Generate gateway token automatically |
| **Daemon auto-install** | Systemd/launchd transparent; starts on reboot |

### The "Hatch" Experience (Killer UX)

When user chooses TUI after onboarding:
1. Prompt: "Wake up, my friend!" (or custom bootstrap message)
2. Agent awakens with identity context
3. Full conversation in terminal (streaming)
4. Web UI opens in background with auth token
5. User can continue in either interface

**Design brilliance:** First interaction is "awakening the agent," not "configuring the system." Emotional hook that increases user investment.

---

## Specific Adoptions (Prioritized)

### Immediate (High ROI)

1. **QuickStart vs Manual Flow**
   - `zerg onboard --quick` → auto-detect workspace, use env keys, defaults
   - `zerg onboard` → full wizard with choices
   - Reduces decision fatigue for first-time users

2. **Graceful Degradation**
   - UI works without API key (shows sessions, can't chat)
   - Features unlock as keys are added
   - No "edit .env and restart" loop

3. **TUI Prompter Abstraction**
   - Use `@clack/prompts` or Python equivalent (`questionary`, `rich`)
   - Abstract interface allows web/voice variants later
   - Testable: mock prompter in unit tests

4. **First-Run "Awakening" Ritual**
   - Frame setup as meeting your agent, not configuring software
   - `zerg hatch` → interactive first conversation
   - Emotional investment from minute one

### Medium-term

5. **Profile-based Credentials**
   - `~/.zerg/auth-profiles.json` instead of flat .env
   - Supports multiple API keys per provider
   - `lastGood` tracking for automatic failover

6. **Daemon Lifecycle Management**
   - `zerg connect --install` already does launchd (shipper)
   - Extend to full Zerg daemon for background agents
   - Health probes, auto-recovery, status command

7. **Non-Interactive Mode (CI/Docker)**
   - `zerg onboard --non-interactive --openai-key $KEY`
   - Deterministic, scriptable
   - Used in Docker images, automated deploys

### Long-term

8. **Install Script**
   - `curl -fsSL https://swarmlet.com/install.sh | bash`
   - Detect OS, install deps (Docker/Python), bootstrap
   - One command from zero to running

---

## Open Questions

1. **Demo mode:** Can we show the Sessions UI with sample data, no API key needed?
2. **OAuth vs API keys:** Should we support Google/GitHub OAuth for LLM providers that offer it?
3. **Hosted quick-start:** Would `swarmlet.com/demo` (hosted instance) be faster than local install?
4. **Brand voice:** How much StarCraft theming is too much?
5. **Python vs Node for CLI:** Moltbot uses Node; Zerg backend is Python. Consistency vs ecosystem?

---

## Zerg vs Moltbot Comparison

| Aspect | Moltbot | Zerg (Current) | Gap |
|--------|---------|----------------|-----|
| **Entry point** | `moltbot onboard` | `make dev` | CLI-first vs dev-first |
| **Time to value** | ~2-3 min | ~10+ min | Need QuickStart flow |
| **Default path** | QuickStart (90s) | None | Add `zerg onboard --quick` |
| **Risk acknowledgement** | Up-front, explicit | None | Add security notice |
| **Daemon install** | Automatic (systemd/launchd) | Docker Compose | Shipper does launchd already |
| **Multi-provider auth** | Profile registry | Env vars | Adopt profile pattern |
| **First interaction** | "Wake up, my friend!" | Cold start | Add awakening ritual |
| **Graceful degradation** | Full (UI works without keys) | None (needs OPENAI_API_KEY) | Priority fix |

---

## Reference Links

- [VISION.md](/VISION.md) — Product vision and architecture
- [Moltbot GitHub](https://github.com/moltbot/moltbot) — Competitor analysis
- [Moltbot Landing](https://www.molt.bot/) — Landing page reference
- [Current README](/README.md) — What we're replacing

---

## Changelog

- **2026-01-29:** Added deep dive findings from Moltbot codebase exploration
- **2026-01-29:** Initial doc created from HN/README analysis session
