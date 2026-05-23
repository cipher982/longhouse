# Longhouse

Mission control for CLI agent sessions running on machines you own. One searchable timeline for Claude Code, Codex, Antigravity, and OpenCode sessions, with live control where the provider CLI supports it. Legacy Gemini sessions remain searchable.

Works on your laptop. Shines on a machine that stays on.

## Install

**macOS (recommended):** download [Longhouse for macOS](https://longhouse.ai/download/macos). Open the app to finish setup.

**Shell installer** (Linux, WSL, or Mac without the app):

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse onboard
```

**Power users:**

```bash
uv tool install longhouse
longhouse onboard
```

All three install the same product. The shell installer also drops `Longhouse.app` into `/Applications` on macOS.

## First Session

```bash
longhouse claude       # managed session, steerable later
longhouse codex        # same, for Codex CLI
longhouse antigravity  # managed observe-only launch
longhouse opencode     # managed observe-only launch
```

Bare `claude`, `codex`, `antigravity`, and `opencode` still get ingested into the timeline — they just stay unmanaged (searchable, not steerable).

The web UI lives at `http://localhost:8080`. The same surface is scriptable:

```bash
longhouse wall --json
longhouse recall "that bug with the flock"
longhouse tail <session-id>
```

## Durable Self-Host

A laptop runtime stops when the laptop sleeps. For real durability, run the Runtime Host on an always-on box (VPS, homelab, Mac mini) and point your dev machines at it.

**On the always-on box:**

```bash
longhouse serve --host 0.0.0.0 --domain longhouse.example.com
```

**On each dev machine:**

```bash
longhouse connect --domain longhouse.example.com --install
```

Set `LONGHOUSE_PASSWORD_HASH` before binding beyond localhost. For TLS, put Caddy in front — `reverse_proxy 127.0.0.1:8080` is the whole config.

Or skip running the box — hosted (we run the Runtime Host for you) is available at <https://control.longhouse.ai/signup>.

## Repair

```bash
longhouse doctor              # diagnose
longhouse upgrade             # update CLI
longhouse machine repair      # repair a configured machine
longhouse connect --install   # first install or force reinstall
```

`longhouse --help` lists every subcommand. Full docs: <https://longhouse.ai/docs>.

## Architecture

Two public components, one product:

- **Machine Agent** — Rust engine on each dev machine. Ships session events.
- **Runtime Host** — FastAPI + bundled web UI + SQLite. Lives where durability should live.

On a laptop both run together for trial use. For durability, separate them. See `VISION.md` for the full product thesis.

## Open Core

This repository is the Apache-2.0 Longhouse core: CLI, Machine Agent, Runtime Host, web UI, self-hosting, and client surfaces over the same machine contracts.

Longhouse Cloud's hosted signup, billing, provisioning, fleet operations, and deployment automation are proprietary and live outside this repository. The public Runtime Host can integrate with a hosted control plane by URL, but self-hosted Longhouse does not require it.

See `EDITIONS.md` and `TRADEMARKS.md` for the boundary.

## Contributing

```bash
git clone https://github.com/cipher982/longhouse.git
cd longhouse
make dev        # backend + frontend with hot reload
make test       # unit tests
make test-e2e   # end-to-end
```

Issues: <https://github.com/cipher982/longhouse/issues>

## Status

Alpha. Actively developed. Claude Code, Codex, Antigravity, and OpenCode sessions sync today; legacy Gemini imports remain supported. Managed launch is live for Claude, Codex, Antigravity, and OpenCode (Antigravity and OpenCode are observe-only at the live-control layer), and the native iOS companion can page on `needs_user` / `blocked` once APNs is configured.

Built by [David Rose](https://github.com/cipher982). Apache-2.0.

<!-- readme-test: verifies install from source and health endpoint -->
```readme-test
{
  "name": "longhouse-serve-health",
  "mode": "smoke",
  "workdir": ".",
  "timeout": 240,
  "env": {
    "AUTH_DISABLED": "1",
    "SKIP_DEMO_SEED": "1"
  },
  "steps": [
    "bun install --frozen-lockfile --silent",
    "(cd web && bun run build)",
    "uv venv .tmp-readme-serve-venv --python 3.12 -q",
    ". .tmp-readme-serve-venv/bin/activate",
    "uv pip install -e server -q",
    "DATABASE_URL=sqlite:///$(mktemp -d)/test.db LLM_DISABLED=1 longhouse serve --port 47398 &",
    "SERVER_PID=$!",
    "for _ in $(seq 1 20); do curl -sf http://127.0.0.1:47398/api/health && break; sleep 1; done",
    "curl -sf http://127.0.0.1:47398/api/health",
    "kill $SERVER_PID 2>/dev/null || true"
  ],
  "cleanup": [
    "pkill -f 'longhouse serve.*47398' 2>/dev/null || true",
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
    "cd {{WORKDIR}}/server && uv sync",
    "cd {{WORKDIR}}/server && HOME={{WORKDIR}}/.qa-home LLM_DISABLED=1 uv run longhouse serve --host 127.0.0.1 --port 8080 --daemon",
    "sleep 5",
    "curl -fsS http://127.0.0.1:8080/api/health",
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
