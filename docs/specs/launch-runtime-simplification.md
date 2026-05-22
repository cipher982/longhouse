# Launch Runtime Simplification

Status: Active
Owner: launch/runtime story
Updated: 2026-04-03

## Goal

Lock Longhouse's launch story to one honest product loop:

- bring every session into one timeline immediately
- keep a control channel open when sessions start through Longhouse
- do this from browser, CLI, or API without pretending a transcript is the whole environment

This document tightens the product story around the session kernel work already captured in `VISION.md`, the current landing-page spec and implementation, the launch demo contract, and the 2026-04-02 landing rewrite note in the Obsidian session log.

## The Decision

The product is **not**:

- an AI dashboard
- a transcript-sync product that magically turns laptop work into cloud work
- a generic remote shell manager
- a taxonomy of `managed-local`, `unmanaged-local`, and `managed-cloud`

The product **is**:

**Longhouse turns real Claude/Codex runs into durable, controllable sessions.**

That is the wedge. The important part is not just that the process survives. The important part is that the user or another agent can still **address and steer the session after launch**.

## Why This Needs Tightening

Recent product work moved in the right direction:

- `VISION.md` now makes the session the durable object, makes CLI/API the real boundary, and demotes MCP to an adapter.
- `docs/specs/landing-session-kernel-mvp.md` correctly moved the site away from "dashboard SaaS" and toward a machine-first session-kernel story.
- The current landing page in `web/src/pages/LandingPage.tsx` and related landing components now foregrounds the machine surface, proof-of-value journey, and kernel thesis.
- The 2026-04-02 landing note correctly identified the strongest sentence so far: `Find the session. Ask it. Continue it.`

But the launch story is still at risk of drifting into two bad frames:

1. **Transcript teleportation**
   "Ship your laptop session to the cloud and Longhouse continues it there."

2. **Remote shell parity**
   "Run Claude on a box and reconnect later."

The first is dishonest for real development. The second is not enough to beat `ssh` + `tmux`.

## Current Pain

The product needs one explicit "why now" before it explains architecture.

Today, the typical user is doing some ugly mix of:

- Claude history / provider UIs that do not make sessions addressable
- `.resume` and provider-local session recovery
- manual `rg` / JSONL grepping through old logs
- `ssh` + `tmux` for keeping one shell alive
- copy-pasting between sessions or between devices

All of those solve a slice of the problem. None of them turn the session into a durable object that can be found, inspected, messaged, and continued from more than one surface.

## Product Truth

### What Longhouse actually controls

Longhouse is strongest when it has all three:

1. **Session identity**
   A stable `session_id`, transcript, presence, and addressing handle.
2. **Environment handle**
   The session still lives where the real work lives.
3. **Control path**
   Longhouse can inject, continue, or coordinate that session after launch.

If Longhouse only has the transcript, that session is still useful for archive/search/inspection, but it is not the full product loop.

### What this means in practice

Longhouse should assume:

- sessions run on the machine where Longhouse is installed
- users may install Longhouse on a laptop, VPS, Mac mini, homelab box, or future hosted instance
- users can still ship/import existing sessions, and that is often the fastest first-value path
- the distinction is capability, not object type: when Longhouse is in the launch path, the session stays reachable later through live control, host reattach, or an explicit cloud branch

This keeps the environment honest. It avoids promising that untracked files, `.env`, local networking, ports, or machine-specific setup somehow moved just because the transcript did.

## Outcome First, Identity Second

Two statements are both true, but they should not be used in the same place:

- **Outcome statement:** `Control your Claude/Codex sessions after launch.`
- **Identity statement:** `Longhouse is a session kernel.`

The outcome statement belongs in the hero, README top matter, and launch demos.

The identity statement belongs in technical docs, the second scroll of the landing page, README explanation sections, and the developer mental model.

## Relationship To `VISION.md`

This spec is a narrowing pass, not a contradiction.

The key `VISION.md` moves are still right:

- **The session is the durable object**
- **Agents talk to each other through sessions**
- **CLI/API-first public primitives**
- **MCP is an adapter, not the boundary**
- **Longhouse as the bundled product around the session-control kernel**

What changes here is the **launch emphasis**:

- do not lead with a big endgame taxonomy
- do not lead with "cloud session migration"
- do not lead with the archive alone
- lead with control of real sessions after launch

`VISION.md` currently still contains some "local to cloud transition" language that is useful strategically but too broad for the launch story. Public launch copy should use the narrower truth:

**Install Longhouse where sessions should live. Start them there. Then control them from anywhere.**

## Relationship To The Current Landing Work

### What the current landing gets right

The current landing direction is materially better than the old framing.

From the spec in `docs/specs/landing-session-kernel-mvp.md`, the current React landing, and the 2026-04-02 session note, the following should stay:

- the shift from "AI dashboard" to "session kernel"
- the machine surface section
- the proof-of-value journey
- honest provider truth
- hosted as convenience, not the first gate
- the bundled web UI as a human view over the machine seam

### What should change

The current landing still overweights:

- "free local" language
- archive/context recovery as the first emotional hook
- the session-kernel explanation before the painkiller

The page should keep the current structure, but the top-level promise should get sharper.

The line `Find the session. Ask it. Continue it.` is strong and should remain. But it works best as the **mechanic line**, not the full emotional headline. It describes what the product does after the user already buys the premise.

The stronger premise is:

**Longhouse lets you control live Claude/Codex sessions after launch.**

That makes the "find / ask / continue" loop feel like proof instead of abstraction.

## The Launch Story

### Two-beat onramp

**Beat 1: every session lands in one timeline**

Install Longhouse and immediately get value from sessions you already have:

- one searchable timeline
- session detail and raw transcript
- recall / search / wall / tail
- no workflow change required

This is the fastest "oh cool" moment and should remain a co-equal first beat, not a fallback.

**Beat 2: Longhouse keeps the control path attached**

Start a real Claude or Codex session through Longhouse on the machine where work should live. Later, find that session, message it, and either steer it live, reopen it on the host, or branch from its synced context honestly.

That machine might be:

- your laptop
- a VPS
- a Mac mini
- a homelab box
- a future Longhouse-hosted instance

The machine choice is secondary. The session loop is the product.

### One honest sentence

**Every session lands in one timeline. When Longhouse is in the launch path, that session stays reachable through an explicit control capability.**

## Capability, Not Type

Longhouse should not teach users that there are different species of sessions.

The correct model is:

- every item in the timeline is a session
- Longhouse-first launch adds a control path to that session
- capability changes state, not ontology

Good product language:

- `This session has live control`
- `This session can continue on the web`
- `This session needs host reattach`
- `This session is history/search only right now`

Bad product language:

- `Longhouse session` vs `imported session` as if they are different objects
- `managed` / `unmanaged` as the primary user-facing distinction
- any UI that swaps whole layouts as if the user is looking at a different thing

## Interaction Rule

Session surfaces should stay structurally consistent.

That means:

- the same timeline card shape
- the same session detail layout
- the same dock/composer presence
- disabled or explanatory actions when Longhouse cannot currently drive the session

Do not hide the core surface just because capability is lower. Explain the limitation in-place.

## Public Vocabulary

Use the smallest vocabulary possible.

### Good public terms

- **session**
- **timeline**
- **live control**
- **Self-hosted**
- **Hosted**
- **Machine surface**
- **Bundled human view**

### Internal-only terms

- `managed-local`
- `unmanaged`
- `managed-cloud`
- runtime-placement taxonomy
- any internal harness names like `commis`

### Important wording rule

Avoid making "always-on machine" sound mandatory.

The product should welcome laptop users while quietly making it obvious that a VPS, Mac mini, or homelab box is the best fit for durable unattended work.

Good phrasing:

- `Run it on your laptop, your machine, or ours.`
- `Install Longhouse where your sessions should live.`
- `Self-host free or use hosted later.`
- `Works on your laptop. Shines on a machine that stays on.`

Bad phrasing:

- `You must run this on an always-on box.`
- `Start free locally` as the only framing
- anything that makes laptop use sound second-class

## Why This Is Better Than `ssh` + `tmux`

This point needs to stay brutally clear.

If the user only wants one remote shell, `ssh` + `tmux` is simpler and probably better.

Longhouse wins only when the session becomes a **first-class object** instead of a pane:

- stable identity
- searchable transcript
- session detail and tail
- message delivery and inbox state
- direct continuation path
- control from browser, CLI, or API
- coordination between sessions

`ssh` keeps a terminal alive.

Longhouse keeps the **session addressable and steerable** after launch.

That is the moat. The ability to inject and continue real Claude/Codex sessions after launch is the core hack and must remain central to the story.

## Launch-Ready Capability Truth

The launch story only works if capability truth is blunt.

### What we can say confidently

- Claude Code sessions are the strongest launch-ready continuation path.
- Claude, Codex, Antigravity, and legacy Gemini sessions can all be shipped/imported into the archive and inspected through the timeline plus machine surface.
- The machine surface for search, session inspection, wall/tail, messaging, and inbox state is real and should be shown early.

### What we should not overstate

- Codex and Antigravity continuation should not be sold as equal to Claude until parity and polish are actually there.
- Transcript import alone should not be sold as if it grants full remote control.
- Hosted should not be sold as the thing that makes the core loop possible; it is the convenience deployment of the same loop.

## What To De-Emphasize

For launch, push the following down the page or out of the first impression:

- broad "agent platform" language
- a named assistant layer as a primary noun
- MCP as a setup requirement
- abstract "agent memory" positioning
- hosted control-plane complexity
- transcript-shipping as if it were full environment migration
- too much provider matrix detail in the hero

These things can still exist, but they should not define the first 30 seconds.

## Golden User Journeys

### Journey 1: laptop tryout

- install Longhouse
- import or ship existing sessions
- find it later
- continue or inspect it from the UI or CLI

This proves the product without requiring infra.

### Journey 2: self-hosted durable box

- install Longhouse on a VPS / Mac mini / homelab box
- start sessions through Longhouse there
- reconnect from another device later
- message and continue those sessions remotely

This is the strongest real-world loop and should anchor demos and launch videos.

### Journey 3: hosted later

- same product loop
- we provision the machine for the user
- convenience, not a different product category

Hosted should be explained as "we run the box," not as an entirely different ontology.

## Launch Feature Triage

### Must demo

- import or ship existing sessions into the timeline
- search and session detail
- one real control-after-launch proof on a Claude session
- one machine-surface proof such as `wall`, `tail`, `message`, or `continue`

### Should work, but not hero

- proactive operator/deputy behavior
- insights / recall depth
- jobs / runner / broader orchestration
- hosted provisioning flow

### Mention as roadmap, not launch promise

- full continuation parity beyond Claude
- TUI attach / remote attach ergonomics
- richer multi-agent coordination flows

### Activation / polish, not hero copy

- launch ergonomics on top of explicit `longhouse claude` / `longhouse codex` entrypoints

## Product Boundaries

### What we should promise

- Longhouse can observe CLI sessions.
- Longhouse can turn Longhouse-launched sessions into durable, addressable endpoints.
- Longhouse can search, inspect, message, and continue sessions through CLI/API/UI.
- Longhouse is strongest when it runs on the machine where the real session environment lives.

### What we should not promise

- seamless migration of arbitrary laptop work into another runtime
- perfect provider parity today
- that transcript sync alone equals continuation
- that hosted is required for first value

## Success Signals

The launch story is working if users do both beats:

1. **Findable first value**
   A new user gets a session into Longhouse and reaches session detail/search quickly.

2. **Controllable second value**
   That same user performs at least one real control action after launch:
   continue, message, or another explicit session-level interaction.

The activation signal to care about is not just install count. It is the conversion from timeline visibility into one real post-launch control action.

## Concrete Guidance For Landing / README / Demos

### Hero

Lead with control, not taxonomy.

Recommended direction:

- headline about controlling live Claude/Codex sessions after launch
- supporting line that says every session lands in one timeline immediately
- supporting line that says the session can live on your laptop, your machine, or hosted later
- use `Works on your laptop. Shines on a machine that stays on.` somewhere high on the page
- keep `Find the session. Ask it. Continue it.` as the proof loop

### Machine seam

Keep the CLI/API examples early. This is one of the strongest parts of the current landing direction.

### Free vs hosted

Prefer:

- `Self-host free`
- `Hosted later`

Over:

- `free local`
- `cloud beta` as the main product noun

### Demo path

The canonical demo should prove:

1. an existing session becomes visible and useful immediately
2. Longhouse can find and inspect it later
3. Longhouse can steer a real session after launch

That beats both dashboard theater and remote-shell theater.

## Immediate Implications For Product Work

1. Keep the machine surface and session-kernel work as the canonical seam.
2. Treat imported/shipped sessions as the first hit of value, not as a separate product class.
3. Reframe `longhouse claude` / `longhouse codex` as "start through Longhouse" rather than implying a different species of session.
4. Keep explicit Longhouse start commands as the supported control-ready path.
5. Keep hosted as a convenience layer that can arrive later without changing the product truth.
6. Keep named assistant layers out of the hero, but do show browser proof that a session can be messaged or continued after launch.

## Summary

The launch story should collapse to this:

**Longhouse turns Claude and Codex runs into durable sessions you can find, message, and continue after launch.**

Users can install it on a laptop, a VPS, a Mac mini, or any machine they control. The more durable the machine, the better the experience, but the product promise stays the same.

Everything else is supporting structure.
