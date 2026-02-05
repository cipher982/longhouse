# Longhouse

Never lose an AI coding conversation.

Search across Claude, Codex, Cursor, Gemini. Resume from anywhere.

## Quick Start

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
# Open http://localhost:8080
```

## Features

- **Timeline**: Searchable archive of all your AI coding sessions
- **Search**: FTS5-powered instant discovery across all sessions
- **Resume**: Continue any session from any device

## Install Options

### 1. One-liner install (Recommended)
```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve --demo  # Try with sample data
```

### 2. pip install (Alternate)
```bash
pip install longhouse
longhouse onboard
longhouse serve --demo  # Try with sample data
```

### 3. Docker
```bash
docker compose up
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

## Documentation

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

**Alpha** - Actively developed, expect changes. Currently works with Claude Code, more tools coming soon.

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
