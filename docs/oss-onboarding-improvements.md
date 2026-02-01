# OSS Onboarding Improvements

**Goal:** Make Longhouse go viral like Moltbot. Someone sees it on HN, tries it, finds value, stars/shares.

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
| Run | `make dev` | `longhouse up` |
| Time to value | ~10 min if lucky | < 2 min |

### What We Actually Offer (Hidden Value)
1. **Sessions Timeline** — All your Claude/Codex/Gemini sessions unified and searchable
2. **Oikos Chat** — AI assistant with 65+ tools
3. **Background Agents** — Commis jobs run async while you work
4. **Shipper** — Real-time sync from laptop to Longhouse ("magic moment")

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

## Longhouse's Unfair Advantages

Things Moltbot doesn't have that we do:

1. **Session archive as product** — We're not just another chat assistant. The unified session timeline IS the product.
2. **Commons brand** — "Longhouse" signals a shared work hall (hub of action + discussion)
3. **Shipper magic** — "Your Claude Code session appears in Longhouse before you switch tabs"
4. **Already built** — Sessions UI, shipper, ingest API all working

---

## Action Plan

### Phase 1: README Rewrite (Now)
- [ ] One-sentence hook: "All your AI coding sessions, unified and searchable"
- [ ] Screenshot of Sessions timeline
- [ ] Lean into Longhouse: "Bring your agents home"
- [ ] Three pathways: Quick / Full / Developer
- [ ] Honest about current state + "coming soon"

### Phase 2: Reduce Install Friction (Next)
- [ ] Create `install.sh` that bootstraps everything
- [ ] Create `longhouse onboard` interactive wizard
- [ ] Add root `docker-compose.yml` that just works
- [ ] Make UI work without API key (demo mode)

### Phase 3: One-Liner Install (Soon)
- [ ] `curl -fsSL https://longhouse.ai/install.sh | bash`
- [ ] Homebrew formula: `brew install longhouse`
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
| One-liner install | `curl -fsSL https://longhouse.ai/install.sh \| bash` |
| Interactive onboard | `longhouse onboard` wizard for API keys, auth |
| Graceful degradation | Show UI even without keys, unlock features progressively |
| Multiple pathways | Quick start / Full setup / Developer |
| Personality | Longhouse theming: "gather", "convene", "craft", "build" |

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

**For Longhouse:** Could adopt this for `~/.longhouse/credentials/` instead of flat env vars

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
   - `longhouse onboard --quick` → auto-detect workspace, use env keys, defaults
   - `longhouse onboard` → full wizard with choices
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
   - `longhouse hatch` → interactive first conversation
   - Emotional investment from minute one

### Medium-term

5. **Profile-based Credentials**
   - `~/.longhouse/auth-profiles.json` instead of flat .env
   - Supports multiple API keys per provider
   - `lastGood` tracking for automatic failover

6. **Daemon Lifecycle Management**
   - `longhouse connect --install` already does launchd (shipper)
   - Extend to full Longhouse daemon for background agents
   - Health probes, auto-recovery, status command

7. **Non-Interactive Mode (CI/Docker)**
   - `longhouse onboard --non-interactive --openai-key $KEY`
   - Deterministic, scriptable
   - Used in Docker images, automated deploys

### Long-term

8. **Install Script**
   - `curl -fsSL https://longhouse.ai/install.sh | bash`
   - Detect OS, install deps (Docker/Python), bootstrap
   - One command from zero to running

---

## README Draft

### Structure (Moltbot-inspired)

```markdown
<p align="center">
  <img src="..." alt="Longhouse" width="200" />
</p>

<h1 align="center">Longhouse</h1>

<p align="center">
  <strong>All your AI coding sessions, unified and searchable.</strong>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#features">Features</a> •
  <a href="#docs">Docs</a> •
  <a href="https://discord.gg/...">Discord</a>
</p>

---

## The Problem

You use Claude Code, Codex, Gemini, Cursor. Each stores sessions in obscure
JSONL files scattered across your filesystem. Want to find that conversation
from last week? Good luck.

## The Solution

Longhouse watches your AI coding sessions and unifies them into a single,
searchable timeline. The session you're having right now? It's already in Longhouse.

[SCREENSHOT: Sessions timeline showing Claude/Codex/Gemini unified]

## Quick Start

### Fastest (2 minutes)
\`\`\`bash
curl -fsSL https://longhouse.ai/install.sh | bash
longhouse up
# Open http://localhost:30080
\`\`\`

### Docker (if you prefer)
\`\`\`bash
git clone https://github.com/cipher982/longhouse && cd longhouse
docker compose up
\`\`\`

### Developer Setup
See [CONTRIBUTING.md](CONTRIBUTING.md) for full development environment.

## Features

- **Unified Timeline** — Claude, Codex, Gemini, Cursor sessions in one view
- **Real-time Sync** — Sessions appear before you switch tabs
- **Full-text Search** — Find any conversation, tool call, or code snippet
- **Background Agents** — Spawn AI agents that work while you're away
- **65+ Tools** — Web search, email, calendar, and more

## How It Works

1. **Shipper** watches `~/.claude/`, `~/.codex/`, etc.
2. **Longhouse** ingests sessions into a unified database
3. **Timeline UI** lets you browse, search, and replay

[DIAGRAM: Simple shipper → Longhouse → UI flow]

## Status

🚧 **Alpha** — Working locally, rough edges remain.

- [x] Session ingestion (Claude, Codex, Gemini)
- [x] Timeline UI
- [x] Background agents
- [ ] One-liner install (coming soon)
- [ ] Hosted option (coming soon)

## License

ISC
```

### Key Changes from Current

| Current | New |
|---------|-----|
| "Supervisor + Workers with unified single-origin UI" | "All your AI coding sessions, unified and searchable" |
| Architecture diagram first | Problem/solution first |
| `make dev` | Three pathways (curl, docker, developer) |
| No screenshot | Screenshot of Sessions timeline |
| 97 lines of implementation details | Progressive disclosure |

---

## Onboarding Wizard Design

### `longhouse onboard` Flow

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   🐙 Welcome to Longhouse                                        │
│                                                             │
│   Longhouse unifies your AI coding sessions into a searchable    │
│   timeline. Let's get you set up.                          │
│                                                             │
│   ◉ Quick Setup (recommended)                              │
│   ○ Custom Setup                                            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Quick Setup Path (~60s)

```
Step 1: LLM Provider
┌─────────────────────────────────────────────────────────────┐
│   Which AI provider do you use most?                        │
│                                                             │
│   ◉ OpenAI (GPT-4, etc.)                                   │
│   ○ Anthropic (Claude)                                      │
│   ○ Google (Gemini)                                         │
│   ○ Skip for now (browse sessions only)                     │
└─────────────────────────────────────────────────────────────┘

Step 2: API Key (if not skipped)
┌─────────────────────────────────────────────────────────────┐
│   Enter your OpenAI API key:                                │
│   (starts with sk-)                                         │
│                                                             │
│   > sk-••••••••••••••••••••••••••••••••                     │
│                                                             │
│   💡 Get one at: https://platform.openai.com/api-keys      │
└─────────────────────────────────────────────────────────────┘

Step 3: Session Sync
┌─────────────────────────────────────────────────────────────┐
│   Want to sync your existing Claude Code sessions?          │
│                                                             │
│   Found: ~/.claude/projects/ (127 sessions)                │
│                                                             │
│   ◉ Yes, start syncing                                     │
│   ○ No, I'll do this later                                 │
└─────────────────────────────────────────────────────────────┘

Step 4: Done
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   ✓ Longhouse is ready!                                         │
│                                                             │
│   Starting server...                                        │
│   → Web UI: http://localhost:30080                         │
│   → Sessions: 127 synced                                   │
│                                                             │
│   Press Enter to open in browser, or Ctrl+C to exit.       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Custom Setup Path (adds)

- Port configuration
- Database choice (SQLite-only target; Postgres is current runtime)
- Auth setup (for multi-user)
- Advanced shipper options

### Implementation Notes

**Python TUI library:** Use `questionary` (simple) or `textual` (rich)

```python
import questionary

flow = questionary.select(
    "Setup mode:",
    choices=["Quick Setup (recommended)", "Custom Setup"]
).ask()

if flow == "Quick Setup (recommended)":
    provider = questionary.select(
        "Which AI provider?",
        choices=["OpenAI", "Anthropic", "Google", "Skip for now"]
    ).ask()

    if provider != "Skip for now":
        key = questionary.password(f"Enter your {provider} API key:").ask()
        # Validate key, store in ~/.longhouse/credentials.json
```

---

## Graceful Degradation Design

### Feature Unlock Matrix

| Feature | No API Key | With API Key |
|---------|------------|--------------|
| View sessions timeline | ✅ | ✅ |
| Search sessions | ✅ | ✅ |
| View session details | ✅ | ✅ |
| Shipper (sync sessions) | ✅ | ✅ |
| Oikos chat | ❌ (prompt to add key) | ✅ |
| Background agents | ❌ (prompt to add key) | ✅ |
| Tool execution | ❌ | ✅ |

### UI Without API Key

```
┌─────────────────────────────────────────────────────────────┐
│  Sessions Timeline                              [+ Add Key] │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Today                                                      │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 🟣 Claude Code — longhouse/backend refactor         │   │
│  │ 45 messages, 23 tool calls • 2 hours ago            │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 🟢 Codex — life-hub dashboard fix                   │   │
│  │ 12 messages, 8 tool calls • 4 hours ago             │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  Yesterday                                                  │
│  ...                                                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘

Clicking "Chat" tab without API key:
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  🔑 API Key Required                                        │
│                                                             │
│  To chat with Oikos, add an API key:                       │
│                                                             │
│  [Add OpenAI Key]  [Add Anthropic Key]  [Add Google Key]   │
│                                                             │
│  Or run: longhouse config --add-key                             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Backend Changes Needed

1. **Startup without key:** Remove `OPENAI_API_KEY` from required env vars
2. **Lazy LLM init:** Only initialize LLM client when first chat/agent request
3. **API error handling:** Return 402 (Payment Required) or custom error when LLM needed but no key
4. **Frontend gate:** Check for key presence, show unlock prompt instead of error

---

## Longhouse Branding Guide

**Theme:** A shared work hall for action + discussion.

### Terminology (light touch)

| Generic | Longhouse |
|---------|-----------|
| Workspace | Workroom |
| Group | Crew |
| Meeting | Convene |
| Start | Begin |

### Where to Use

- **Marketing copy:** “gather,” “convene,” “workroom,” “hall”
- **First-run message:** “Welcome to Longhouse — your shared work hall”
- **README:** Light touch, not overwhelming

### Where NOT to Use

- CLI commands (keep neutral: `longhouse up`, `longhouse onboard`)
- API endpoints and schemas
- Error codes or low-level logs

### Example: First Run

```
🐙 Spawning your swarm...

✓ Hive initialized at ~/.longhouse/
✓ Overmind listening on :30080
✓ 127 sessions detected, syncing...

Your swarm is ready. Open http://localhost:30080
```

---

## Install Script Design

### `https://longhouse.ai/install.sh`

```bash
#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "🐙 Installing Longhouse..."

# Detect OS
case "$OSTYPE" in
  darwin*)  OS="macos" ;;
  linux*)   OS="linux" ;;
  *)        echo "${RED}Unsupported OS: $OSTYPE${NC}"; exit 1 ;;
esac

# Check dependencies
check_dep() {
  if ! command -v $1 &> /dev/null; then
    echo "${YELLOW}Installing $1...${NC}"
    return 1
  fi
  return 0
}

# Install Docker if missing (current Postgres runtime; remove when SQLite-only ships)
if ! check_dep docker; then
  if [ "$OS" = "macos" ]; then
    echo "Please install Docker Desktop: https://docker.com/products/docker-desktop"
    exit 1
  else
    curl -fsSL https://get.docker.com | sh
  fi
fi

# Install Longhouse CLI
if ! check_dep longhouse; then
  if check_dep pipx; then
    pipx install longhouse
  elif check_dep pip; then
    pip install --user longhouse
  else
    echo "${RED}Please install Python/pip first${NC}"
    exit 1
  fi
fi

echo "${GREEN}✓ Longhouse installed${NC}"
echo ""
echo "Next steps:"
echo "  longhouse onboard    # Interactive setup"
echo "  longhouse up         # Start the server"
```

### What It Does

1. Detect OS (macOS/Linux)
2. Check for Docker (required for Postgres)
3. Install `longhouse` via pipx/pip
4. Print next steps

### What It Doesn't Do

- Install Python (assume user has it or direct them)
- Run `longhouse onboard` automatically (let user control)
- Require sudo (pipx installs to user space)

---

## Success Metrics

### How We Know This Worked

| Metric | Current | Target | How to Measure |
|--------|---------|--------|----------------|
| **Time to first session view** | ~10 min | < 3 min | Stopwatch test with fresh machine |
| **README bounce rate** | Unknown | < 50% | Umami analytics on repo page |
| **Install completion rate** | Unknown | > 70% | Track `longhouse onboard` completions |
| **GitHub stars** | ~50 | 500+ | GitHub API |
| **HN front page** | No | Yes | Manual check |

### User Journey Checkpoints

1. **Lands on README** — Does hook make sense in 5 seconds?
2. **Tries quick start** — Does it work first try?
3. **Sees sessions** — Is there a "wow" moment?
4. **Tries chat** — Does Oikos respond intelligently?
5. **Returns next day** — Did shipper keep syncing?

### Feedback Collection

- Add `longhouse feedback` command (opens GitHub issue with template)
- Umami events on key actions (onboard complete, first chat, etc.)
- Discord channel for early adopters

---

## Open Questions

1. **Demo mode:** Can we show the Sessions UI with sample data, no API key needed?
2. **OAuth vs API keys:** Should we support Google/GitHub OAuth for LLM providers that offer it?
3. **Hosted quick-start:** Would `longhouse.ai/demo` (hosted instance) be faster than local install?
4. **Brand voice:** How much Longhouse theming is too much?
5. **Python vs Node for CLI:** Moltbot uses Node; Longhouse backend is Python. Consistency vs ecosystem?

---

## Longhouse vs Moltbot Comparison

| Aspect | Moltbot | Longhouse (Current) | Gap |
|--------|---------|----------------|-----|
| **Entry point** | `moltbot onboard` | `make dev` | CLI-first vs dev-first |
| **Time to value** | ~2-3 min | ~10+ min | Need QuickStart flow |
| **Default path** | QuickStart (90s) | None | Add `longhouse onboard --quick` |
| **Risk acknowledgement** | Up-front, explicit | None | Add security notice |
| **Daemon install** | Automatic (systemd/launchd) | Docker Compose | Shipper does launchd already |
| **Multi-provider auth** | Profile registry | Env vars | Adopt profile pattern |
| **First interaction** | "Wake up, my friend!" | Cold start | Add awakening ritual |
| **Graceful degradation** | Full (UI works without keys) | None (needs OPENAI_API_KEY) | Priority fix |

---

## Implementation Roadmap

### Week 1: Foundation

| Task | Files | Effort |
|------|-------|--------|
| README rewrite | `README.md` | 2h |
| Screenshot Sessions UI | `apps/zerg/frontend-web/branding/` | 30m |
| Add root docker-compose.yml | `docker-compose.yml` | 1h |
| Graceful degradation (backend) | `apps/zerg/backend/zerg/main.py`, `config.py` | 4h |
| Graceful degradation (frontend) | `apps/zerg/frontend-web/src/pages/Chat.tsx` | 2h |

### Week 2: Onboarding

| Task | Files | Effort |
|------|-------|--------|
| Create `longhouse` CLI package | `apps/zerg/cli/` (new) | 4h |
| `longhouse onboard` wizard | `apps/zerg/cli/onboard.py` | 6h |
| `longhouse up` command | `apps/zerg/cli/up.py` | 2h |
| Credential storage | `apps/zerg/cli/credentials.py` | 3h |
| Integrate with shipper CLI | `apps/zerg/backend/zerg/cli/` | 2h |

### Week 3: Polish

| Task | Files | Effort |
|------|-------|--------|
| Install script | `scripts/install.sh` | 3h |
| Host install.sh on longhouse.ai | Coolify config | 1h |
| Demo mode (sample data) | `apps/zerg/backend/zerg/demo/` | 4h |
| Landing page update | External (longhouse.ai) | 4h |

### Week 4: Launch

| Task | Owner | Notes |
|------|-------|-------|
| Test on fresh macOS | Manual | Full flow test |
| Test on fresh Ubuntu | Manual | Docker flow test |
| Write HN post | David | Focus on "unified sessions" angle |
| Post to HN | David | Timing matters |
| Monitor feedback | David | First 24h critical |

---

## File Change Summary

### New Files

```
apps/zerg/cli/                    # New CLI package
├── __init__.py
├── __main__.py                   # Entry point
├── onboard.py                    # Interactive wizard
├── up.py                         # Start server
├── credentials.py                # Credential management
└── config.py                     # CLI config

scripts/install.sh                # One-liner installer
docker-compose.yml                # Root-level compose file
```

### Modified Files

```
README.md                         # Complete rewrite
apps/zerg/backend/zerg/
├── main.py                       # Remove required OPENAI_API_KEY
├── config.py                     # Make LLM keys optional
└── services/llm_service.py       # Lazy initialization

apps/zerg/frontend-web/src/
├── pages/Chat.tsx                # Add "no key" prompt
├── components/ApiKeyPrompt.tsx   # New component
└── hooks/useApiKeyStatus.ts      # Check key presence
```

---

## Reference Links

- [VISION.md](/VISION.md) — Product vision and architecture
- [Moltbot GitHub](https://github.com/moltbot/moltbot) — Competitor analysis
- [Moltbot Landing](https://www.molt.bot/) — Landing page reference
- [Current README](/README.md) — What we're replacing

---

## Changelog

- **2026-01-29:** Added implementation roadmap, file changes, README draft, onboard wizard design
- **2026-01-29:** Added deep dive findings from Moltbot codebase exploration
- **2026-01-29:** Initial doc created from HN/README analysis session
