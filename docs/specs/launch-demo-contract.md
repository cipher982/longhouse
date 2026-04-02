# Launch Demo Contract

Status: Active
Owner: launch/runtime story
Updated: 2026-04-02

## Goal

Freeze one launch demo that proves the real product loop without drifting into parity theater, dashboard fluff, or fake cloud-transcript migration.

The demo must prove two beats in order:

1. existing sessions become findable
2. new Longhouse sessions become controllable after launch

## One Sentence

**Your existing sessions become findable. Your new Longhouse sessions become controllable.**

## Three-Minute Demo

### 1. Install and start Longhouse

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
```

Say:

`Longhouse runs where the sessions should live. Laptop is fine. A box that stays on is better.`

### 2. Import existing sessions immediately

```bash
longhouse connect --install
longhouse ship
```

Say:

`I do not need to change my workflow first. My existing Claude, Codex, and Gemini sessions become searchable right away.`

### 3. Find a prior solution

- open the timeline
- search for a real prior topic like auth, retries, or a refactor
- open one session

Say:

`This is the first beat: findable sessions, not dead logs or a provider-specific history pane.`

### 4. Show the machine surface

```bash
longhouse wall --json
```

Optional:

```bash
longhouse tail <session-id>
```

Say:

`The same session is visible from the browser, CLI, and API. It is an addressable object, not just a tmux pane.`

### 5. Show control after launch

- continue or message a real Claude session from Longhouse
- show the session responding after it was already running

Say:

`This is the second beat. Longhouse does not just keep a shell open. It lets me steer the session after launch.`

### 6. Close with deployment truth

Say:

`This works on a laptop. It shines on a machine that stays on. Self-hosted is free. Hosted is the convenience version later.`

## Must Show

- existing sessions visible quickly
- one raw session detail view
- one CLI machine-surface proof
- one real Claude control-after-launch proof (accepted turn plus Claude hook phases observed)
- the laptop-friendly but durable-machine-better framing

## Must Not Imply

- Codex and Gemini continuation parity today
- hosted required for first value
- transcript sync alone equals full remote control
- Longhouse is just a web dashboard

## Launch-Ready Truth

- Claude Code is the strongest continuation path today.
- Launch proof for Claude means a managed Longhouse session accepts a turn and Claude hook phases confirm the session actually worked that turn.
- Claude, Codex, and Gemini all import into the archive and machine surface today.
- Codex and Gemini continuation are roadmap, not the launch promise.

## Supporting Surfaces

These surfaces should all tell the same story:

- `README.md`
- landing hero and proof journey
- docs quick-start
- onboarding wizard

If one of those leads with demo data instead of existing-session import, it is drifting from the launch contract.
