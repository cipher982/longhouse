# Landing Page Spec: Session Kernel MVP

Status: draft
Owner: launch/runtime story
Updated: 2026-04-02

## Goal

Make the landing page sell the prelaunch wedge clearly:

- existing sessions become findable immediately
- new Longhouse sessions become controllable after launch
- Longhouse is a session kernel for coding agents, but that is the technical identity, not the first emotional hook
- The bundled web UI is real, but it sits on top of a machine-first product seam.
- Hosted is the paid convenience layer for always-on access, not the first required step.

## Audience

Primary:
- solo developers already using Claude Code, Codex CLI, or Gemini CLI
- technical early adopters who are comfortable with terminals and APIs
- users who already feel the pain of losing context across agent sessions
- users who already have a pile of existing Claude/Codex work and want value without changing workflow first

Secondary:
- curious builders who want a strong local demo before trusting a hosted beta

## Core Promise

Public outcome:

**Control Claude/Codex sessions after launch.**

Mechanic line:

**Find the session. Ask it. Continue it.**

Longhouse makes existing sessions findable now and new Longhouse sessions controllable after launch.

## Message Hierarchy

1. **Existing sessions become findable**
   Search, inspect, and recover context from work the user already did.
2. **New Longhouse sessions become controllable**
   Start a session and steer it after launch from UI, CLI, or API.
3. **Session kernel is the technical identity**
   Longhouse is not just a dashboard. It makes sessions addressable and reusable.
4. **Machine surface is real**
   Wall, tail, peers, messages, inbox, continue.
5. **Hosted when you want convenience**
   Hosted is "we run the box," not a different product category.

## Current Pain

Make the pain concrete before explaining the architecture:

- provider histories are fragmented
- `.resume` and JSONL grep are rough tools for real recovery
- `ssh` + `tmux` keeps a pane alive but not an addressable session
- users lose context or resort to manual copy-paste between devices and sessions

## Demo Journey To Sell

This should be the canonical demo story for videos, README, and landing copy.
See `docs/specs/launch-demo-contract.md` for the concrete launch walkthrough.

1. Install Longhouse locally or on a box you control.
2. Open the timeline with demo sessions or, preferably, shipped real sessions.
3. Search for a prior solution such as auth, retry logic, or a refactor.
4. Open session detail and show the raw transcript / tool history.
5. Show one kernel primitive like `longhouse wall --json` or a directed session message.
6. Continue a real Claude session from Longhouse.
7. Optional final beat: show the same session reachable from another device.

## Section Order

1. Hero
   Headline around control after launch.
   Supporting proof that existing sessions become findable first.
   Primary CTA: `Self-Host Free`
   Secondary CTA: `Hosted Later`
   Tertiary proof: one CLI example visible above the fold.

2. Kernel Thesis
   Three short cards:
   - session is the durable object
   - CLI/API-first primitives
   - works on your laptop, shines on a machine that stays on

3. Proof-of-Value Journey
   A numbered walkthrough showing import -> search -> inspect -> coordinate -> continue.

4. Kernel Surface
   Small code-first section with:
   - `longhouse wall --json`
   - `longhouse tail ...`
   - `longhouse continue ...`
   - `/api/agents/*` as the canonical machine namespace

5. Integrated Human View
   Show timeline, search, and session-detail screenshots as the bundled UI on top of the kernel.

6. Honest Provider Truth
   Claude-first continuation today.
   Codex and Gemini searchable / inspectable today.
   Continuation parity is roadmap, not launch promise.

7. Hosted Adds
   Always-on convenience, browser access from anywhere, your own subdomain.
   Explicit note that hosted is not required for first use.

8. Final CTA
   Repeat local install command.

## Visual Direction

- Editorial / technical hybrid, not generic SaaS gradient-glass.
- Warm paper background, darker ink panels, restrained accent color.
- Serif headline, monospace code surfaces, compact technical labels.
- Screenshot imagery lower on the page than it is today.
- The page should feel like a product manifesto with a real terminal underneath it.

## Copy Rules

- Lead with outcome first, mechanism second.
- Lead with verbs like `find`, `inspect`, `message`, `continue`.
- Avoid implying that Longhouse is primarily a big click-around dashboard.
- Avoid implying that hosted signup is required before the user can feel the product.
- Avoid pretending provider parity is perfect.
- Avoid hand-wavy "AI productivity" copy.
- Use `session kernel` as the technical identity, not the headline users must decode first.
- Use `Works on your laptop. Shines on a machine that stays on.` somewhere in the page flow.

## Must Show

- self-hosted install command
- immediate value from existing sessions
- one real control-after-launch proof
- one machine-first example
- one honest provider-capability section
- one proof-of-value journey
- hosted as upgrade / convenience

## Must Not Imply

- credit card required for first value
- email verification required for local demo
- Oikos is the main product boundary
- Longhouse is a custom agent harness competing with Claude/Codex/Gemini
- hosted beta is more polished than it is
- transcript shipping alone equals full remote control

## Launch-Ready Capability Truth

Say this bluntly in docs and on the page:

- Claude Code is the strongest launch-ready continuation path.
- Claude, Codex, and Gemini can all be imported/shipped into the archive and inspected.
- Codex and Gemini continuation should not be sold as parity features until they are actually polished.

## Launch Feature Triage

### Must demo

- existing sessions become visible and searchable
- raw session detail
- one real Claude continuation or message/control proof
- one CLI/API machine-surface proof

### Nice to show lower on the page

- recall / insights depth
- Oikos as operator/deputy
- hosted convenience layer

### Roadmap only

- continuation parity beyond Claude
- richer multi-agent orchestration
- TUI attach / default wrapper ergonomics

## Success Signals

- a new user gets from install to first useful session in under a few minutes
- that user performs one real session-level control action after launch
- demos make Journey 2 legible in under 3 minutes

## Source Of Truth

- The live React landing page is now the source of truth.
- Do not reintroduce a separate static prototype unless the real app cannot carry the experiment.
