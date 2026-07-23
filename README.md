# Longhouse

Keep using your coding agents like normal. Longhouse watches their native session data and puts Claude Code, Codex, Antigravity, OpenCode, and Cursor sessions into one searchable timeline across the machines you own.

Launch a provider through Longhouse when you want its supported remote-control path. Longhouse keeps the upstream CLI and its terminal UI; the available controls depend on the provider's native seam rather than a pretend one-size-fits-all wrapper.

![Longhouse timeline — one searchable view of your coding-agent sessions across providers and machines](web/public/images/landing/timeline-preview.png)

## Why

If you run coding agents often it gets messy quick across the terminal tabs and transcripts. Today that history is scattered across `~/.claude`, terminal scrollback, or one local log dir per tool.

Longhouse fixes that:

- **Find any past session in seconds** — one timeline + full-text search across every provider and machine.
- **Control live work remotely** — launch a session through Longhouse, then send, interrupt, steer, or resume it later when that provider supports the operation.
- **Own your history** — runs on machines you control, SQLite at the core, nothing uploaded to a vendor cloud.

Longhouse does not replace a provider with its own agent runtime or terminal UI. A bare provider CLI stays observable through its native archive. A managed launch such as `longhouse claude` keeps the stock terminal experience while adding Longhouse's provider-specific control path. The timeline exposes the controls a session can actually perform instead of assuming every provider can steer a live turn.

## Install

**macOS (recommended):** download [Longhouse for macOS](https://longhouse.ai/download/macos). Open the app to finish setup.

**Shell installer** (Linux, WSL, or Mac without the app):

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse auth --url https://your-runtime.example
longhouse machine repair --repair-service
```

The shell installer installs the native pair. On macOS it also drops
`Longhouse.app` into `/Applications`; open it to finish setup. Runtime Host
operators who deliberately need the Python server compatibility CLI install
`longhouse-python` separately in that server environment.

## First Session

```bash
longhouse claude       # managed channel session: send, interrupt, steer, resume
longhouse codex        # managed app-server session: send, interrupt, steer, resume
longhouse opencode     # managed server session: send, interrupt, reattach (not active-turn steer)
```

OpenCode Helm supports send, interrupt, and terminate but not active-turn steer or pause-answer.

Bare provider CLI sessions still get ingested into the timeline — they stay unmanaged: searchable and observable, but without Longhouse-owned remote control.

The web UI lives at `http://localhost:8080`. Runtime Host administration is a
separate server lane and uses its explicit compatibility entrypoint:

```bash
longhouse-python wall --json
longhouse-python recall "that auth refresh bug from last week"
longhouse-python tail <session-id>
```

## Durable Self-Host

A laptop runtime stops when the laptop sleeps. For real durability, run the Runtime Host on an always-on box (VPS, homelab, Mac mini) and point your dev machines at it.

**On the always-on box** — a public bind requires auth, so set it up first:

```bash
export LONGHOUSE_PASSWORD_HASH="$(longhouse-python hash-password)"   # prompts for a password
export JWT_SECRET=$(openssl rand -hex 32)
export INTERNAL_API_SECRET=$(openssl rand -hex 32)

longhouse-python serve --host 0.0.0.0 --domain longhouse.example.com
```

**On each dev machine:**

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse auth --url https://longhouse.example.com
longhouse machine repair --repair-service
```

Binding beyond localhost without auth is refused by default — `longhouse-python serve` exits and tells you what to set. The three exports above are the whole requirement: a password hash plus two random secrets. (If a trusted reverse proxy already authenticates requests, pass `--allow-public-no-auth` to accept the risk.) For TLS, put Caddy in front — `reverse_proxy 127.0.0.1:8080` is the whole config.

Or skip running the box — hosted (we run the Runtime Host for you) is available at <https://control.longhouse.ai/signup>.

## Repair

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash  # install or upgrade the native pair
longhouse local-health --fast --json                   # diagnose
longhouse machine repair                               # restart a configured machine
longhouse machine repair --repair-service              # install/repair its native service
```

`longhouse --help` lists every subcommand. Full docs: <https://longhouse.ai/docs>.

## Architecture
- **Machine Agent** — Rust engine on each dev machine. Ships session events.
- **Runtime Host** — FastAPI + bundled web UI + SQLite. Lives where durability should live.

On a laptop both run together for trial use. But you will want a VPS you self host or just pay me $5 and i will do it. See `VISION.md` for the full product thesis.

## How It Compares

There are great tools for spinning up sandboxed cloud agents, and the model labs now ship their own single-vendor dashboards. Longhouse sits in a different spot: the sessions you *already run*, on hardware you own, across every provider.

## Self-host (free) vs Hosted (paid)

The Apache-2.0 core in this repo is fully usable on your own machines.

Hosted (<https://control.longhouse.ai/signup>) exists for people who don't want to run an always-on box. We run the Runtime Host for you: always-on durability, zero-setup multi-machine sync, and iOS push when a session needs you, the things a sleeping laptop can't do. Same product but we just operate the box.

## Contributing

```bash
git clone https://github.com/cipher982/longhouse.git
cd longhouse
make dev        # backend + frontend with hot reload
make test       # unit tests
make test-e2e   # end-to-end
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, test tiers, and the codegen flow, and [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system map and a glossary of the project's nouns (managed vs unmanaged, Machine Agent vs Runtime Host, wall, recall, …).

Issues: <https://github.com/cipher982/longhouse/issues>

## Status

Alpha. Actively developed. Claude Code, Codex, Cursor, OpenCode, and Antigravity sessions sync today. Native Helm currently supports Claude, Codex, and OpenCode; Cursor and Antigravity remain Shadow-only until their complete native control runtimes exist. The native iOS companion can page on `needs_user` / `blocked` once APNs is configured.

Built and maintained by [David W. Rose](https://drose.io/)
([cipher982](https://github.com/cipher982)). Apache-2.0.

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
    "uv pip install -e server -q",
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
    "cd {{WORKDIR}}/server && HOME={{WORKDIR}}/.qa-home LLM_DISABLED=1 uv run longhouse serve --host 127.0.0.1 --port 8080 --daemon",
    "sleep 5",
    "python3 -c 'import json,urllib.request; p=json.load(urllib.request.urlopen(\"http://127.0.0.1:8080/api/health\")); assert p.get(\"status\") == \"healthy\", p'",
    "cd {{WORKDIR}}/e2e && bun install --silent && PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 bunx playwright test --config playwright.onboarding.config.js --project onboarding-chromium"
  ],
  "cleanup": [
    "cd {{WORKDIR}}/server && HOME={{WORKDIR}}/.qa-home uv run longhouse serve --stop || true",
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
