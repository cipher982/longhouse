# Launch Demo Rehearsal

Status: Active
Owner: launch/runtime story
Updated: 2026-04-02

## Purpose

Turn the launch contract into an operator-ready script that can be rehearsed
without inventing the flow in real time.

This is not a marketing mockup. It is the exact loop we should be able to run
on a self-hosted Longhouse instance before launch day.

## Audience

- David as live demo operator
- launch-week QA before push/deploy
- anyone validating that the landing/README story matches the real product

## Demo Promise

The demo must prove two beats in order:

1. existing sessions become findable
2. new Longhouse sessions become controllable after launch

## Preconditions

Before rehearsing, the machine should already have:

- Longhouse installed and serving locally
- at least one imported real session from Claude, Codex, or Gemini
- one real Longhouse-managed Claude session that can accept a follow-up turn
- `longhouse wall --json` working against the running instance

If any of those are missing, the demo is not ready and the gap should be fixed
before polishing copy.

## Preflight Checklist

- `curl -sf http://127.0.0.1:8080 >/dev/null` or equivalent local UI reachability
- `curl -sf http://127.0.0.1:47300/api/health` on dev or the equivalent runtime health endpoint
- `longhouse wall --json` returns at least one row
- imported sessions are visible in the timeline
- one managed Claude session exists and is safe to continue/message

## Operator Script

### 1. Start with the install truth

Show:

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
```

Say:

`Longhouse runs where the sessions should live. Laptop is fine. A machine that stays on is better.`

### 2. Show the import-first onramp

Show:

```bash
longhouse connect --install
longhouse ship
```

Say:

`I do not need to change my workflow first. My existing Claude, Codex, and Gemini sessions become findable immediately.`

### 3. Prove the first beat in the UI

Show:

- timeline/session list
- search for a real prior topic
- open raw session detail

Say:

`This is the first beat: not dead logs, not one provider's history pane, but real sessions I can find again.`

### 4. Prove the machine surface

Show:

```bash
longhouse wall --json
```

Optional:

```bash
longhouse tail <session-id>
```

Say:

`The same session exists in the browser, CLI, and API. It is a real addressable object, not just a tmux pane.`

### 5. Prove control after launch

Show:

- a real Claude Longhouse session that already exists
- continue or message that session from Longhouse
- the session responds after it was already running

Say:

`This is the second beat. Longhouse does not just keep a shell open. It lets me steer the session after launch.`

### 6. Close with deployment truth

Say:

`This works on your laptop. It shines on a machine that stays on. Self-hosted is free. Hosted is just the convenience version later.`

## Hard Truth To Keep Visible

- Claude is the launch-ready control path today.
- Codex and Gemini are already valuable because they import and stay findable.
- The demo must not imply full continuation parity across providers.
- The demo must not imply transcript sync alone equals remote control.

## Internal Proof Commands

These are not public demo steps. They are launch-week proof helpers.

### Managed Claude control proof

Use the managed-local Claude stress harness in control mode when validating the
post-launch control wedge:

```bash
uv run --project server python scripts/managed-local/managed_local_claude_stress.py \
  --base-url http://127.0.0.1:47300 \
  --cwd /absolute/repo/path \
  --count 1 \
  --verification-mode control
```

Pass means:

- the session accepts the turn
- Claude hook phases observe `thinking` and then a terminal phase such as `idle`

### Transcript durability proof

Use full verification only when the transcript shipper and Claude config are
known to point at the same Longhouse instance:

```bash
uv run --project server python scripts/managed-local/managed_local_claude_stress.py \
  --base-url http://127.0.0.1:47300 \
  --cwd /absolute/repo/path \
  --count 1 \
  --verification-mode full
```

Pass means:

- control proof passes
- the prompt also lands in the transcript DB exactly once

## Rehearsal Pass Criteria

The launch demo is considered rehearsed only when:

- the flow completes without improvising different commands
- the first beat is shown before the second beat
- the operator never has to explain away provider parity confusion
- the control step uses a real already-running Claude session
- the story still lands if the machine is a laptop, without pretending laptop and always-on are the same experience

## If The Demo Feels Weak

Do these in order:

1. fix the product or onboarding gap
2. tighten the operator script
3. tighten the landing/README copy

Do not solve a weak demo with more architecture language.
