# Longhouse

**Remote control for your coding agents.**

→ **[longhouse.ai](https://longhouse.ai)** · [Download for macOS](https://longhouse.ai/download/macos) · [Hosted](https://control.longhouse.ai/signup) · [Docs](https://longhouse.ai/docs)

Watch any Claude Code, Codex, Cursor, or OpenCode session live from the web. Search everything they've done. Send your next instruction to a session launched through Longhouse while the agent keeps running in its real terminal on your machine. Apache-2.0 open core.

![Longhouse timeline — one searchable view of your coding-agent sessions across providers and machines](web/public/images/landing/timeline-preview.png)

## Why

If you run coding agents often it gets messy quick across the terminal tabs and transcripts. Today that history is scattered across `~/.claude`, terminal scrollback, or one local log dir per tool.

Longhouse fixes that:

- **Find any past session in seconds** — one timeline + full-text search across every provider and machine.
- **Control live work remotely** — launch a session through Longhouse, then send, interrupt, steer, or resume it later when that provider supports the operation.
- **Own your history** — Longhouse stores its archive in SQLite on your Runtime Host. The provider client still makes its normal requests to its provider.

Longhouse does not replace a provider with its own agent runtime or terminal UI. A bare provider CLI stays observable through its native archive. A managed launch such as `longhouse claude` keeps the stock terminal experience while adding Longhouse's provider-specific control path. The timeline exposes the controls a session can actually perform instead of assuming every provider can steer a live turn.

## Install

**macOS (recommended):** download [Longhouse for macOS](https://longhouse.ai/download/macos). Open the app to finish setup.

**Shell installer** (Linux, WSL, or Mac without the app):

```bash
curl -fsSL https://get.longhouse.ai/install.sh | LONGHOUSE_URL=https://you.longhouse.ai bash
```

The shell installer installs the native pair, asks what existing history to
import (below), stores the Runtime Host address, opens it in a browser to
approve this machine, and starts the Machine Agent. On macOS it also drops
`Longhouse.app` into `/Applications`. An empty timeline shows this line with
its own address filled in. Runtime Host operators install `longhouse-server`
in that server environment.

**No Longhouse address yet?** Run one on this machine (Linux or macOS, about a minute):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # skip if you already have uv
uv tool install longhouse                          # the Runtime Host: longhouse-server
longhouse-server onboard                           # starts it, installs the Machine Agent, asks what history to import
```

It serves `http://127.0.0.1:8080` and stops when the machine does; a trial. Use the
self-host steps below for one that stays up, or hosted (invited addresses only for now).

### What gets imported

Old transcripts can hold code and secrets from any project you ever ran an
agent in, so a machine that connects imports **only sessions that start from
now on** unless you choose otherwise. The installer asks (and falls back to
"from now on" with no terminal); `LONGHOUSE_IMPORT_SCOPE=now|all|2026-09-01`
answers it non-interactively. Change it any time; widening backfills what
became eligible:

```bash
longhouse machine scope                        # the current scope and what it leaves out
longhouse machine scope --project ~/git/app    # also import one project's full history
longhouse machine scope --since all            # everything on this computer
```

Sessions outside the scope stay on your computer and are not uploaded.
Narrowing it later stops further imports; what is already uploaded or queued is
not recalled. Machines that were already shipping history before scopes existed
keep shipping all of it. Details: [`longhouse machine scope`](https://longhouse.ai/docs/cli).

### Uninstall

```bash
longhouse uninstall --dry-run   # what would be removed
longhouse uninstall             # revoke this machine's token, stop the service,
                                # remove hooks and binaries (--purge also deletes ~/.longhouse)
```

`longhouse auth --clear` alone revokes this machine's device token on the
Runtime Host and deletes the stored credentials. Sessions already uploaded stay
in your Runtime Host's archive until you delete them there. To cut off a
machine you cannot reach, revoke it under Settings, Devices on the Runtime Host.

## First Session

```bash
longhouse claude       # managed Claude Code session
longhouse codex        # managed Codex app-server session
longhouse opencode     # managed OpenCode server session
longhouse cursor       # managed Cursor PTY session
longhouse pi           # managed Pi TUI session
longhouse omp          # managed Oh My Pi TUI session
longhouse antigravity  # managed Antigravity hook session
```

Managed sessions keep the provider's native client and local identity while
adding Longhouse's session-scoped control path. Bare provider sessions remain
Shadow: searchable and observable, but not remotely controlled by Longhouse.
Console is the no-terminal path for sending a turn to a connected machine.

Provider behavior is intentionally capability-specific and changes with the
native clients. Read [Provider Integrations](https://longhouse.ai/docs/integrations)
for the current control and archive details; this README stays focused on the
product shape rather than promising every provider/version/configuration
combination.

The web UI lives at `http://localhost:8080`. Runtime Host administration is a
separate server lane and uses `longhouse-server`:

```bash
longhouse-server status --json
longhouse-server recall "that auth refresh bug from last week"
longhouse-server tail <session-id>
```

## Durability

A laptop runtime stops when the laptop sleeps. For real durability, run the Runtime Host on an always-on box (VPS, homelab, Mac mini) and point your dev machines at it.

| | Self-host | Hosted |
|---|---|---|
| You operate | Runtime Host on a VPS, homelab, or Mac mini | Nothing — we run it |
| Cost | Free (Apache-2.0) | $20/mo |
| Setup | `longhouse-server serve` (steps below) | [control.longhouse.ai/signup](https://control.longhouse.ai/signup) |
| Always-on | Up to you | Yes |
| iOS push on `needs_user` | Yes (APNs config required) | Yes |

**Self-host — on the always-on box:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # skip if you already have uv
uv tool install longhouse                          # installs longhouse-server

export LONGHOUSE_PASSWORD_HASH="$(longhouse-server hash-password)"   # prompts for a password
export JWT_SECRET=$(openssl rand -hex 32)
export INTERNAL_API_SECRET=$(openssl rand -hex 32)

longhouse-server serve --host 0.0.0.0 --domain longhouse.example.com
```

**On each dev machine:**

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash   # asks what history to import
longhouse auth --url https://longhouse.example.com
longhouse machine repair --repair-service
```

**Over Tailscale, no https needed.** If the always-on box and your laptop share a tailnet, skip the domain and the proxy: Tailscale already encrypts the link, so native clients accept plain `http://` to a Tailscale address (`100.x.y.z`, an `fd7a:115c:a1e0::` address, or a `*.ts.net` name).

```bash
# on the always-on box (it prints its "Tailscale:" address)
longhouse-server serve --host 0.0.0.0

# on each dev machine
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse auth --url http://100.x.y.z:8080    # or http://my-box.your-tailnet.ts.net:8080
longhouse machine repair --repair-service
```

The same holds for the macOS app and the iPhone app. Anything else over plain `http://` is refused: a LAN address (`192.168.x`, `10.x`, `172.16-31.x`, `*.local`) works only when you opt in (`longhouse auth --url http://192.168.1.20:8080 --allow-insecure-http`, or `LONGHOUSE_ALLOW_INSECURE_HTTP=1`; it is remembered with the address and warns every time it is used), and a public address needs `https://`.

Binding beyond localhost without auth is refused by default — `longhouse-server serve` exits and tells you what to set. The three exports above are the whole requirement: a password hash plus two random secrets. (If a trusted reverse proxy already authenticates requests, pass `--allow-public-no-auth` to accept the risk.) For TLS, put Caddy in front — `reverse_proxy 127.0.0.1:8080` is the whole config.

## Repair

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash  # install or upgrade the native pair
longhouse local-health --json                             # diagnose
longhouse machine repair                               # restart a configured machine
longhouse machine repair --repair-service              # install/repair its native service
```

`longhouse --help` lists the device commands; `longhouse-server --help` lists the Runtime Host ones. Full docs: <https://longhouse.ai/docs>.

## What makes Longhouse different

Other tools spin up sandboxed cloud agents or wrap a single vendor's dashboard. Longhouse unifies the sessions you already run, on hardware you own, across providers. You keep using the official clients and provider plans you already have instead of buying access to another model-backed coding agent.

## Status

Actively developed pre-release. Longhouse currently supports Claude Code, Codex,
Cursor, OpenCode, Antigravity, Pi Agent, and OMP across archive and managed
control paths. The exact operation support is capability-specific; see
[Provider Integrations](https://longhouse.ai/docs/integrations) and the
in-product provider view for current details. This README is not a
compatibility matrix.

The iOS client lives in `ios/` and handles APNs push on `needs_user`. Install the beta on
an iPhone from TestFlight: <https://testflight.apple.com/join/CmEd5kY7> (or tap "Explore the
demo" on its first screen to look around without an account).

See [RELEASE.md](RELEASE.md) for how releases are cut.

Built and maintained by [David W. Rose](https://drose.io/)
([cipher982](https://github.com/cipher982)). Apache-2.0.

## Architecture

- **Machine Agent** — Rust engine on each dev machine. Ships session events.
- **Runtime Host** — FastAPI + bundled web UI + SQLite. Lives where durability should live.

On a laptop both run together for trial use. For the full system map, component detail, and a glossary of the project's nouns (Shadow/Helm/Console, wall, recall, peers, …) see [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`VISION.md`](VISION.md).

For code navigation, start with `server/`, `web/`, and `engine/` for the core
runtime; `ios/`, `desktop/`, and `runner/` for clients and support; and
`schemas/`, `config/`, and `scripts/` for contracts and tooling. The
[contributor guide](CONTRIBUTING.md#project-layout) has the complete tree.

## Contributing

```bash
git clone https://github.com/cipher982/longhouse.git
cd longhouse
make dev        # local UI with hot reload against your linked Runtime Host
make dev-demo   # isolated local backend + seeded demo UI
make test       # unit tests
make test-e2e   # end-to-end
```

Good entry points: web timeline UI, additional provider-CLI ingest parsers, CLI subcommand UX, and docs. Look for [`good first issue`](https://github.com/cipher982/longhouse/labels/good%20first%20issue) labels.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, test tiers, the codegen flow, and the open-core boundary. [`EDITIONS.md`](EDITIONS.md) has the line between the Apache-2.0 core and Longhouse Cloud.

Issues: <https://github.com/cipher982/longhouse/issues>

---

→ **[longhouse.ai](https://longhouse.ai)**

<!-- readme-test: verifies install from source and health endpoint -->
```readme-test
{
  "name": "longhouse-serve-health",
  "mode": "smoke",
  "workdir": ".",
  "timeout": 600,
  "env": {
    "AUTH_DISABLED": "1",
    "SKIP_DEMO_SEED": "1"
  },
  "steps": [
    "bun install --frozen-lockfile --silent",
    "(cd web && bun run build)",
    "python3 scripts/build/generate_build_identity.py",
    "uv venv .tmp-readme-serve-venv --python 3.12 -q",
    ". .tmp-readme-serve-venv/bin/activate",
    "uv sync --frozen --active --no-dev --project server --quiet",
    "scripts/qa/readme-serve-health-smoke.sh"
  ],
  "cleanup": [
    "rm -rf .tmp-readme-serve-venv"
  ]
}
```

<!-- onboarding-contract:start -->
```json
{
  "workdir": "/tmp/longhouse-onboarding",
  "steps": [
    "cd {{WORKDIR}}/web && bun install --silent && bun run build",
    "cd {{WORKDIR}} && python3 scripts/build/generate_build_identity.py",
    "cd {{WORKDIR}}/server && uv sync",
    "cd {{WORKDIR}}/server && HOME={{WORKDIR}}/.qa-home LLM_DISABLED=1 uv run longhouse-server serve --host 127.0.0.1 --port 8080 --daemon",
    "sleep 5",
    "python3 -c 'import json,urllib.request; p=json.load(urllib.request.urlopen(\"http://127.0.0.1:8080/api/health\")); assert p.get(\"status\") == \"healthy\", p'",
    "cd {{WORKDIR}}/e2e && bun install --silent && PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 bunx playwright test --config playwright.onboarding.config.js --project onboarding-chromium"
  ],
  "cleanup": [
    "cd {{WORKDIR}}/server && HOME={{WORKDIR}}/.qa-home uv run longhouse-server serve --stop || true",
    "rm -rf {{WORKDIR}}/.qa-home"
  ],
  "primary_route": "/timeline",
  "cta_buttons": [
    {
      "label": "Machines",
      "selector": "button:has-text(\"Machines\")"
    }
  ]
}
```
<!-- onboarding-contract:end -->
