# Longhouse

Never lose an AI coding conversation.

Search and browse every Claude Code, Codex CLI, and Gemini CLI session. Self-host or hosted.

## Demo

<!-- Video: Replace this section with a Loom/YouTube embed once the walkthrough is recorded.
     Target: 60-90 second tour covering install -> timeline -> search. -->

Video walkthrough coming soon. In the meantime, try it yourself:

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve --demo
```

## Get Started

### Hosted (beta)

Sign up at https://longhouse.ai. Hosted unlocks always-on sync and resume from any device (Claude sessions today).

### Self-host (local)

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
# Open http://localhost:8080
```

## Features

- **Timeline**: Searchable archive of all your AI coding sessions
- **Search**: FTS5-powered instant discovery across all sessions (launch requirement)
- **Resume**: Continue Claude Code sessions (hosted or self-hosted)

## Install Options

### 0. Hosted (beta)
Get started at https://longhouse.ai (Google OAuth + Stripe checkout).

### 1. One-liner install (Recommended)
```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve --demo  # Try with sample data
```

See [Install Guide](docs/install-guide.md) for what the installer does, onboarding wizard details, and manual install options.

### 2. pip install (Alternate)
```bash
pip install longhouse
longhouse onboard
longhouse serve --demo  # Try with sample data
```

### 3. Docker
```bash
docker compose -f docker/docker-compose.dev.yml up
```

### 4. From source
```bash
git clone https://github.com/cipher982/longhouse
cd longhouse && make dev
```

## Configuration

Set `LONGHOUSE_PASSWORD` (plaintext) or `LONGHOUSE_PASSWORD_HASH` (recommended) for remote access authentication.

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
longhouse serve --demo   # Start with sample data
longhouse serve --demo-fresh # Rebuild demo data on start
longhouse connect    # Sync Claude Code sessions (continuous)
longhouse ship       # One-time sync
longhouse status     # Show configuration
longhouse onboard    # Re-run setup wizard
```

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

### `claude: command not found` when using hooks

Claude Code must be installed separately. The `longhouse connect --install` command will warn you if `claude` is not on PATH in a fresh shell.

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

### Reinstalling or upgrading

```bash
uv tool upgrade longhouse
longhouse doctor   # verify
```

## Documentation

See the [Install Guide](docs/install-guide.md) for detailed setup instructions, onboarding wizard walkthrough, and troubleshooting.

This README is the canonical user guide. Product direction lives in `VISION.md`, and the execution roadmap lives in `TODO.md`.

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

**Alpha** - Actively developed, expect changes. Claude Code, Codex CLI, and Gemini CLI sessions ship today; hosted beta in progress.

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
    "cd {{WORKDIR}}/apps/zerg/frontend-web && bun install --silent && bun run build",
    "cd {{WORKDIR}}/apps/zerg/backend && uv sync",
    "cd {{WORKDIR}}/apps/zerg/backend && HOME={{WORKDIR}}/.qa-home uv run longhouse serve --demo-fresh --host 127.0.0.1 --port 8080 --daemon",
    "sleep 5",
    "curl -fsS http://127.0.0.1:8080/api/health"
  ],
  "cleanup": [
    "cd {{WORKDIR}}/apps/zerg/backend && HOME={{WORKDIR}}/.qa-home uv run longhouse serve --stop || true",
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
