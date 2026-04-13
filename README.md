# Longhouse

Longhouse puts Claude Code, Codex CLI, and Gemini CLI sessions into one searchable timeline and keeps a control channel open when they start through Longhouse.

Import sessions you already have, or route new work through Longhouse, then inspect, message, and keep working from the web UI, CLI, or HTTP.

A session stays the same object either way. Longhouse in the launch path changes what you can do with that session later; it does not create a second class of session.

Works on your laptop. Shines on a machine that stays on.

Self-host free on the machine where work should live, or use hosted beta later when you want us to run the Longhouse runtime for you. Claude is the strongest continuation path today; Codex and Gemini are searchable and inspectable today.

## Demo

<!-- Video: Replace this section with a Loom/YouTube embed once the walkthrough is recorded.
     Target: 60-90 second tour covering install -> search/detail -> wall/message -> continue. -->

Video walkthrough coming soon. In the meantime, the first run is simple:

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
```

The installer runs guided onboarding, starts the local Longhouse runtime, sets up the background machine agent when supported, and on macOS installs `Longhouse.app` for local status. Open `http://localhost:8080` and find one prior session.

When you want control after launch:

```bash
longhouse claude
longhouse codex
```

If you need repair later, start with `longhouse doctor`.

The web UI is the easiest place to look around, but the session surface is scriptable too:

```bash
longhouse wall --json
```

If you want a safe preview before importing real work:

```bash
longhouse serve --demo
```

## Get Started

### Try it out (laptop)

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
```

Open `http://localhost:8080`. This runs both the Machine Agent and the Runtime Host on your laptop — good for trying the product, but it stops when your laptop sleeps.

On macOS the installer also adds `Longhouse.app` to your menu bar.

### macOS app download (Apple Silicon)

If you want the non-terminal path on a modern Mac, download the desktop app directly:

[Download Longhouse for macOS](https://longhouse.ai/download/macos)

### Self-host for durability (always-on machine)

For durable session storage, run the Runtime Host on an always-on box (VPS, homelab, Mac mini) and point your laptop's Machine Agent at it:

```bash
# On your always-on box:
curl -fsSL https://get.longhouse.ai/install.sh | bash

# On your dev machine(s), point the agent at the server:
longhouse onboard  # choose "remote" or "existing server"
```

### Start control-ready sessions

```bash
longhouse claude
longhouse codex
```

### Hosted beta (later)

Sign up at https://longhouse.ai when you want the convenience path — we run the Runtime Host for you, your dev machines only need the Machine Agent.

## Features

- **Find existing sessions fast**: Import and search old Claude, Codex, and Gemini work immediately
- **Control after launch**: Start through Longhouse to keep a live control path or host reattach path available later
- **One timeline**: Claude Code, Codex CLI, and Gemini CLI sessions in one place
- **Search + recall**: Find messages, tool calls, file edits, and session metadata fast
- **Agent-first coordination**: Read the wall, tail sessions, find peers, and send directed session messages by CLI or API
- **Self-hosted or hosted**: Self-host free on an always-on machine, or use hosted beta later

## Install Options

### Shell installer (recommended)
```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
```

Installs the `longhouse` CLI, runs onboarding, sets up the Machine Agent, and on macOS adds `Longhouse.app`. Set `LONGHOUSE_NO_WIZARD=1` to skip the wizard.

### macOS desktop app

For a click-first install on Apple Silicon Macs, use the direct desktop download:

[Download Longhouse for macOS](https://longhouse.ai/download/macos)

### With `uv`
```bash
uv tool install longhouse
longhouse onboard
```

### Repair and upgrade

```bash
longhouse doctor              # Self-diagnosis
longhouse upgrade             # Upgrade the installed CLI
longhouse connect --install   # Repair hooks and machine agent
```

Use `serve --demo` for a safe preview before importing real work.

### 3. Advanced / contributor paths

Docker is mainly for CI and contributor workflows, not the primary end-user install path.

```bash
docker compose -f docker/docker-compose.dev.yml up
```

From source:

```bash
git clone https://github.com/cipher982/longhouse
cd longhouse && make dev
```

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
    "DATABASE_URL=sqlite:///$(mktemp -d)/test.db longhouse serve --port 47398 &",
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

## Configuration

### Local defaults

- Local UI: `http://localhost:8080`
- Local database: `~/.longhouse/longhouse.db`
- Local quickstart auth: disabled by default on localhost

### Remote or shared access

Set `LONGHOUSE_PASSWORD` (plaintext) or `LONGHOUSE_PASSWORD_HASH` (recommended) before binding beyond localhost.

### Gmail Inbox Setup (Self-hosted)

The inbox Gmail flow is BYO Google config on self-hosted installs. Users will see setup errors until the instance has:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GMAIL_PUBSUB_TOPIC`
- `PUBSUB_AUDIENCE`

Set `PUBSUB_SA_EMAIL` too if you want the webhook to pin Pub/Sub push auth to a specific service account.

Generate a pbkdf2 hash:
```bash
python - <<'PY'
import base64, hashlib, os
password = "change-me"
salt = os.urandom(16)
iterations = 200_000
dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
print(f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}")
PY
```

## Commands

```bash
longhouse serve      # Start the local runtime
longhouse serve --demo        # Start with sample data
longhouse serve --demo-fresh  # Rebuild demo data on start
longhouse connect             # Run the machine agent in foreground
longhouse connect --install   # Install hooks + background machine agent
longhouse ship                # One-time import pass
longhouse wall --json         # Read raw coordination signals
longhouse peers --json        # Find nearby live peer sessions
longhouse tail ...            # Read recent events from a session
longhouse message ...         # Send a durable directed session message
longhouse recall              # Search and find sessions
longhouse sessions get ...    # Get session details
longhouse sessions events ... # Get session events
longhouse auth                # Manage authentication
longhouse config show         # Show effective configuration
longhouse status              # Show local runtime status and health
longhouse version --check     # Check whether a CLI update is available
longhouse upgrade             # Upgrade the installed CLI
longhouse doctor              # Self-diagnosis
longhouse doctor --check-updates  # Include latest stable CLI check
longhouse onboard             # Re-run setup wizard
longhouse migrate             # Migrate local data to newer format
longhouse claude              # Start Claude Code through Longhouse
longhouse codex               # Start Codex CLI through Longhouse
```

Interactive CLI commands refresh update state in the background and show a
cached upgrade hint when the installed CLI is behind the latest stable release.

For the canonical machine-facing API and copyable coordination recipes, see `docs/specs/agents-machine-surface.md`.

## Troubleshooting

### `longhouse: command not found` after install

The installer adds `~/.local/bin` to your shell profile, but the current terminal may not have picked it up yet.

```bash
# Option 1: reload your shell profile
source ~/.zshrc   # or ~/.bashrc / ~/.bash_profile

# Option 2: add the path manually
export PATH="$HOME/.local/bin:$PATH"
```

Run `longhouse doctor` to verify everything is working.

### Claude Code not found when installing hooks

Claude Code must be installed separately. `longhouse connect --install` will install the machine agent and CLI hooks for Claude Code and Codex when those CLIs are present.

```bash
# Check if claude is reachable
which claude

# If installed but not on PATH, add its directory:
export PATH="/path/to/claude/bin:$PATH"
```

### Local runtime won't start (port in use)

```bash
# Find what's using the port
lsof -i :8080

# Use a different port
longhouse serve --port 8081
```

### Hooks not firing / sessions not shipping

```bash
# Check machine-agent status
longhouse connect --status

# Check doctor for full diagnosis
longhouse doctor

# Manually ship once to test
longhouse ship --verbose
```

`longhouse connect --install` sets up the machine agent and CLI hooks only. It does not modify your normal global Claude/Codex MCP tool menus.

### Reinstalling or upgrading

```bash
longhouse upgrade
longhouse doctor   # verify

# or use the underlying package-manager path directly
uv tool upgrade longhouse
longhouse doctor   # verify
```

For a full disposable install -> upgrade rehearsal without touching your real
machine state:

```bash
make test-install-upgrade
```

## Documentation

- User docs: https://longhouse.ai/docs
- Product direction: `VISION.md`
- Issues and bugs: https://github.com/cipher982/longhouse/issues

---

## For Contributors

Full dev setup with hot reload:

```bash
git clone https://github.com/cipher982/longhouse.git
cd longhouse
make dev  # Starts backend + frontend
```

Run tests:

```bash
make test          # Unit tests
make test-e2e      # End-to-end tests
```

Note: pre-commit hooks may auto-fix files (ruff/ruff-format, etc.). Re-stage before committing.

## Architecture

```
User → CLI (longhouse) → FastAPI backend → SQLite (~/.longhouse/longhouse.db)
                       ↓
                  React frontend (localhost:8080)
```

**Stack:**
- Backend: Python 3.12+, FastAPI, SQLAlchemy, SQLite
- Frontend: React 19, TypeScript, Vite
- CLI: Typer, uv

## Why "Longhouse"?

Traditional longhouses were communal structures where tribes gathered and preserved history. Your Longhouse is where all your AI coding sessions gather and persist.

Each session is a log in your timeline.

## Status

**Alpha**. Actively developed. Claude Code, Codex CLI, and Gemini CLI sessions sync today. Claude is the strongest continuation path today; Codex and Gemini are searchable and inspectable today. Hosted remains the convenience path later, not the required first step.

## Author

Built by [David Rose](https://github.com/cipher982) -- indie developer building AI agent tools.

- GitHub: https://github.com/cipher982
- Twitter/X: https://x.com/cipher982

## License

Apache-2.0 - see LICENSE file

## Links

- **Documentation:** https://longhouse.ai/docs
- **Issues:** https://github.com/cipher982/longhouse/issues
- **PyPI:** https://pypi.org/project/longhouse/

---

Onboarding contract (CI). Do not edit unless the README steps change.

<!-- onboarding-contract:start -->
```json
{
  "workdir": "/tmp/longhouse-onboarding",
  "steps": [
    "cd {{WORKDIR}}/web && bun install --silent && bun run build",
    "cd {{WORKDIR}}/server && uv sync",
    "cd {{WORKDIR}}/server && HOME={{WORKDIR}}/.qa-home uv run longhouse serve --host 127.0.0.1 --port 8080 --daemon",
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
      "label": "See import steps",
      "selector": "button:has-text(\"See import steps\")"
    },
    {
      "label": "Load demo sessions instead",
      "selector": "button:has-text(\"Load demo sessions instead\")"
    }
  ]
}
```
<!-- onboarding-contract:end -->
