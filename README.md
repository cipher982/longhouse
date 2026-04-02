# Longhouse

Longhouse makes Claude Code, Codex CLI, and Gemini CLI sessions findable now and controllable after launch.

Import existing sessions into one searchable timeline, then start Longhouse sessions you can inspect, message, and continue from the web UI, CLI, or HTTP.

Works on your laptop. Shines on a machine that stays on.

Self-host free on the machine where work should live, or use hosted beta later when you want the convenience path. Claude is the strongest continuation path today; Codex and Gemini are searchable and inspectable today.

## Demo

<!-- Video: Replace this section with a Loom/YouTube embed once the walkthrough is recorded.
     Target: 60-90 second tour covering install -> search/detail -> wall/message -> continue. -->

Video walkthrough coming soon. In the meantime, try the real launch loop:

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
longhouse connect --install
longhouse ship
# Open http://localhost:8080
```

The bundled web UI is the easiest way to look around, but the same session object is scriptable:

```bash
longhouse wall --json
```

If you want a safe preview before importing real work:

```bash
longhouse serve --demo
```

## Get Started

### Self-host free

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
```

Open `http://localhost:8080`.

The installer installs the `longhouse` CLI and runs `longhouse onboard` automatically. If you skipped onboarding or want to re-run the import path setup, use:

```bash
longhouse connect --install
longhouse ship
```

Want a safe preview before importing real sessions?

```bash
longhouse serve --demo
```

If you want bare `claude` / `codex` interactive launches to go through Longhouse by default, opt in explicitly:

```bash
longhouse wrap --install
```

Wrapper mode is passthrough-first and reversible. `auth`, `help`, non-interactive flags, and setup failures still fall back to the native CLI.

### Canonical proof journey

1. Install and start Longhouse.
2. Import existing sessions with `longhouse connect --install` and `longhouse ship`.
3. Search for a real prior topic in the timeline.
4. Open raw session detail.
5. Show `longhouse wall --json`.
6. Continue or message a real Claude session after launch.

### Hosted beta (later)

Sign up at https://longhouse.ai when you want the convenience path. Hosted beta is "we run the box" for you; the free first-run path is still the self-hosted installer above, and it is the recommended way to understand the product loop first.

## Features

- **Find existing sessions fast**: Import and search old Claude, Codex, and Gemini work immediately
- **Control after launch**: Start Longhouse sessions you can inspect, address, message, and continue later
- **Session kernel**: The technical model is a durable session object, not a dead transcript
- **One timeline**: Claude Code, Codex CLI, and Gemini CLI sessions in one place
- **Search + recall**: Find messages, tool calls, file edits, and session metadata fast
- **Claude continuation**: Claude Code is the strongest launch-ready continuation path today
- **Agent-first coordination**: Read the wall, tail sessions, find peers, send directed session messages, and manage inbox state by CLI or API
- **Hosted or self-hosted**: Self-host free now or use hosted beta later without changing the core loop

## Install Options

### 0. Self-host with the installer (recommended)
```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
```

The installer installs the `longhouse` CLI and runs `longhouse onboard` automatically. Set `LONGHOUSE_NO_WIZARD=1` to skip the wizard, or rerun it later with `longhouse onboard --quick`.

Import existing sessions right away:

```bash
longhouse connect --install
longhouse ship
```

Preview with sample data only if you want a safe fallback:

```bash
longhouse serve --demo
```

### 1. Self-host with `uv`
```bash
uv tool install longhouse
longhouse onboard
longhouse serve
```

### Optional: default-launcher wrappers

Keep the default install non-invasive, then opt in when you want bare `claude` / `codex` launches to route through Longhouse:

```bash
longhouse wrap --install
longhouse wrap --status
```

Undo at any time:

```bash
longhouse wrap --uninstall
```

### 2. Hosted beta (later)
Get started at https://longhouse.ai when you want the hosted convenience path. Keep the self-hosted installer as the primary free wedge for first use, demos, and durable machine setups.

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
longhouse serve      # Start the server
longhouse serve --demo        # Start with sample data
longhouse serve --demo-fresh  # Rebuild demo data on start
longhouse connect             # Run the shipper/watch process in foreground
longhouse connect --install   # Install hooks + background engine service
longhouse ship                # One-time import pass
longhouse wall --json         # Read raw coordination signals
longhouse peers --json        # Find nearby live peer sessions
longhouse message ...         # Send a durable directed session message
longhouse continue ...        # Continue a session from the machine surface
longhouse check-messages      # Read the durable inbox
longhouse ack-message ...     # Acknowledge a delivered message
longhouse status              # Show effective configuration
longhouse doctor              # Self-diagnosis
longhouse onboard             # Re-run setup wizard
```

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

Claude Code must be installed separately. `longhouse connect --install` will set up shipping hooks for Claude Code and Codex when those CLIs are present.

```bash
# Check if claude is reachable
which claude

# If installed but not on PATH, add its directory:
export PATH="/path/to/claude/bin:$PATH"
```

### Server won't start (port in use)

```bash
# Find what's using the port
lsof -i :8080

# Use a different port
longhouse serve --port 8081
```

### Hooks not firing / sessions not shipping

```bash
# Check shipper status
longhouse connect --status

# Check doctor for full diagnosis
longhouse doctor

# Manually ship once to test
longhouse ship --verbose
```

`longhouse connect --install` sets up shipping and hooks only. It does not modify your normal global Claude/Codex MCP tool menus.

### Reinstalling or upgrading

```bash
uv tool upgrade longhouse
longhouse doctor   # verify
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
    "cd {{WORKDIR}}/server && HOME={{WORKDIR}}/.qa-home uv run longhouse serve --demo-fresh --host 127.0.0.1 --port 8080 --daemon",
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
      "label": "Timeline search",
      "selector": ".sessions-search-input"
    },
    {
      "label": "Timeline sessions",
      "selector": ".session-card"
    }
  ]
}
```
<!-- onboarding-contract:end -->
