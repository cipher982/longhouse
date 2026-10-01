# Longhouse Runner

**Status: support tier.** Functional but not launch-critical. The core product loop (ingest, timeline, search, recall, live control) does not require a Runner.

## What it does

The Runner is a WebSocket daemon that executes shell commands on a user-owned machine on behalf of the Longhouse Runtime Host. It connects to the server, authenticates, and waits for execution requests.

Use cases:
- The Runtime Host can run diagnostic commands on the user's machine
- Future: remote session launch and management

## Security model

Commands are filtered by declared capabilities:

- `exec.readonly` — only allowlisted read-only commands (`ps`, `ls`, `cat`, `docker logs`, etc.)
- `exec.full` — unrestricted shell execution
- `docker` — required for any Docker command

Additional safety rails: 50KB output cap, timeout enforcement, shell metacharacter restrictions in readonly mode.

The Runner authenticates with a shared secret issued when it enrolls: create an enrollment token on the Runtime Host's Runners page, then run the install command it shows.

## When to use

Most users do not need a Runner. Connecting a machine (`longhouse auth`, then `longhouse machine repair --repair-service`) sets up the Machine Agent, which ships sessions and carries remote control. Add a Runner only if the machine should also accept remote commands.

## Dev run

```bash
bun install
export LONGHOUSE_URL=http://localhost:8080
export RUNNER_NAME=my-runner
export RUNNER_SECRET=your_secret_here
bun run --filter @longhouse/runner start
```

## Required env

- `LONGHOUSE_URL` (ws/wss or http/https)
- `RUNNER_NAME` or `RUNNER_ID`
- `RUNNER_SECRET`

Optional: `RUNNER_CAPABILITIES`, `HEARTBEAT_INTERVAL_MS`.

<!-- readme-test: verifies bun install and TypeScript type-check -->
```readme-test
{
  "name": "runner-install-typecheck",
  "mode": "smoke",
  "workdir": "runner",
  "timeout": 120,
  "steps": [
    "bun install --frozen-lockfile",
    "bunx tsc --noEmit"
  ]
}
```
