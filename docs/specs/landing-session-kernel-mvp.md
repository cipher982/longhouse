# Landing Page Spec: Session Kernel MVP

Status: draft
Owner: launch/runtime story
Updated: 2026-04-02

## Goal

Make the landing page sell the prelaunch wedge clearly:

- Longhouse is a session kernel for coding agents.
- The first proof of value is free and local.
- The bundled web UI is real, but it sits on top of a machine-first product seam.
- Hosted is the paid convenience layer for always-on access, not the first required step.

## Audience

Primary:
- solo developers already using Claude Code, Codex CLI, or Gemini CLI
- technical early adopters who are comfortable with terminals and APIs
- users who already feel the pain of losing context across agent sessions

Secondary:
- curious builders who want a strong local demo before trusting a hosted beta

## Core Promise

Find the session. Ask it. Continue it.

Longhouse turns provider session logs into durable objects you can search, inspect, message, and resume.

## Message Hierarchy

1. **Session kernel**
   Longhouse is not just a dashboard. It makes sessions addressable and reusable.
2. **Free first win**
   Install locally, load demo data or real sessions, and get value before billing or account friction.
3. **Recover context fast**
   Search prior work, inspect raw detail, and continue from the exact point that matters.
4. **Coordinate through the kernel**
   Wall, tail, peers, messages, inbox, continue.
5. **Hosted when you want always-on**
   Paid hosted access is the upgrade, not the gate.

## Demo Journey To Sell

This should be the canonical demo story for videos, README, and landing copy.

1. Install Longhouse locally.
2. Open the timeline with demo sessions or shipped real sessions.
3. Search for a prior solution such as auth, retry logic, or a refactor.
4. Open session detail and show the raw transcript / tool history.
5. Show one kernel primitive like `longhouse wall --json` or a directed session message.
6. Continue the current session from Longhouse.
7. Optional final beat: show the same session reachable from another device / hosted canary.

## Section Order

1. Hero
   Headline around the session kernel and fast context recovery.
   Primary CTA: `Start Free Locally`
   Secondary CTA: `Hosted Beta`
   Tertiary proof: one CLI example visible above the fold.

2. Kernel Thesis
   Three short cards:
   - session is the durable object
   - CLI/API-first primitives
   - hosted is convenience, not the product boundary

3. Proof-of-Value Journey
   A numbered walkthrough showing install -> search -> inspect -> coordinate -> continue.

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
   Codex and Gemini archive sync now, cloud-start path now, continuation parity later.

7. Hosted Adds
   Always-on sessions, browser access from anywhere, managed cloud sessions, your own subdomain.
   Explicit note that hosted beta is not required for first use.

8. Final CTA
   Repeat local install command.

## Visual Direction

- Editorial / technical hybrid, not generic SaaS gradient-glass.
- Warm paper background, darker ink panels, restrained accent color.
- Serif headline, monospace code surfaces, compact technical labels.
- Screenshot imagery lower on the page than it is today.
- The page should feel like a product manifesto with a real terminal underneath it.

## Copy Rules

- Lead with verbs like `search`, `inspect`, `message`, `continue`.
- Avoid implying that Longhouse is primarily a big click-around dashboard.
- Avoid implying that hosted signup is required before the user can feel the product.
- Avoid pretending provider parity is perfect.
- Avoid hand-wavy "AI productivity" copy.

## Must Show

- free local install command
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

## Current Prototype

- HTML prototype: `web/public/landing-session-kernel-v2.html`
