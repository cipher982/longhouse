# Launch Runtime Simplification

Status: Active
Owner: launch/runtime story
Updated: 2026-04-02

## Goal

Lock Longhouse's launch story to one honest product loop:

- start a real Claude or Codex session
- find that session later
- message it or continue it after launch
- do this from browser, CLI, or API without pretending a transcript is the whole environment

This document tightens the product story around the session kernel work already captured in `VISION.md`, the current landing-page spec and implementation, and the 2026-04-02 landing rewrite note in the Obsidian session log.

## The Decision

The product is **not**:

- an AI dashboard
- a transcript-sync product that magically turns laptop work into cloud work
- a generic remote shell manager
- a taxonomy of `managed-local`, `unmanaged-local`, and `managed-cloud`

The product **is**:

**Longhouse turns real Claude/Codex runs into durable, controllable sessions.**

That is the wedge. The important part is not just that the process survives. The important part is that the user, another agent, or Oikos can still **address and steer the session after launch**.

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
- users can still ship/import existing sessions, but imported sessions are a compatibility path, not the hero

This keeps the environment honest. It avoids promising that untracked files, `.env`, local networking, ports, or machine-specific setup somehow moved just because the transcript did.

## Relationship To `VISION.md`

This spec is a narrowing pass, not a contradiction.

The key `VISION.md` moves are still right:

- **The session is the durable object**
- **Agents talk to each other through sessions**
- **CLI/API-first public primitives**
- **MCP is an adapter, not the boundary**
- **Longhouse as the integrated distribution**

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

### One hero story

Start a real Claude or Codex session on a machine you control. Later, find it, message it, and continue it from anywhere.

That machine might be:

- your laptop
- a VPS
- a Mac mini
- a homelab box
- a future Longhouse-hosted instance

The machine choice is secondary. The session loop is the product.

### One fallback story

Already using normal Claude/Codex sessions?

Longhouse can still import and index them so you can search, inspect, and learn from them. That is the compatibility onramp, not the hero.

## Public Vocabulary

Use the smallest vocabulary possible.

### Good public terms

- **Longhouse session**
- **Imported session**
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

## What To De-Emphasize

For launch, push the following down the page or out of the first impression:

- broad "agent platform" language
- Oikos as a primary noun
- MCP as a setup requirement
- abstract "agent memory" positioning
- hosted control-plane complexity
- transcript-shipping as if it were full environment migration
- too much provider matrix detail in the hero

These things can still exist, but they should not define the first 30 seconds.

## Golden User Journeys

### Journey 1: laptop tryout

- install Longhouse
- start or import a session
- find it later
- continue or inspect it from the UI or CLI

This proves the product without requiring infra.

### Journey 2: self-hosted durable box

- install Longhouse on a VPS / Mac mini / homelab box
- start Longhouse sessions there
- reconnect from another device later
- message and continue those sessions remotely

This is the strongest real-world loop and should anchor demos and launch videos.

### Journey 3: hosted later

- same product loop
- we provision the machine for the user
- convenience, not a different product category

Hosted should be explained as "we run the box," not as an entirely different ontology.

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

## Concrete Guidance For Landing / README / Demos

### Hero

Lead with control, not taxonomy.

Recommended direction:

- headline about controlling live Claude/Codex sessions after launch
- supporting line that says the session can live on your laptop, your machine, or hosted later
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

1. a real session exists
2. Longhouse can find it later
3. Longhouse can steer it after launch

That beats both dashboard theater and remote-shell theater.

## Immediate Implications For Product Work

1. Keep the machine surface and session-kernel work as the canonical seam.
2. Keep imported sessions as the onramp, not the hero.
3. Reframe `longhouse claude` / `longhouse codex` as "start a Longhouse session" rather than "managed-local launcher" in public copy.
4. Keep wrapper mode as an ergonomic accelerator, not the definition of the product.
5. Keep hosted as a convenience layer that can arrive later without changing the product truth.

## Summary

The launch story should collapse to this:

**Longhouse turns Claude and Codex runs into durable sessions you can find, message, and continue after launch.**

Users can install it on a laptop, a VPS, a Mac mini, or any machine they control. The more durable the machine, the better the experience, but the product promise stays the same.

Everything else is supporting structure.
