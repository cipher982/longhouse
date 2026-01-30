<p align="center">
  <img src="apps/zerg/frontend-web/branding/swarm-logo-master.png" alt="Zerg" width="180" />
</p>

<h1 align="center">Zerg</h1>

<p align="center">
  <strong>All your AI coding sessions, unified and searchable.</strong>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#features">Features</a> •
  <a href="#how-it-works">How It Works</a> •
  <a href="#status">Status</a>
</p>

---

## The Problem

You use Claude Code, Codex, Gemini, Cursor. Each stores sessions in obscure JSONL files scattered across your filesystem. Want to find that conversation from last week? Good luck.

## The Solution

Zerg watches your AI coding sessions and unifies them into a single, searchable timeline. See what you worked on, when, and pick up where you left off.

<!-- Screenshot will go here once we capture it -->
<!-- ![Timeline Screenshot](apps/zerg/frontend-web/branding/timeline-screenshot.png) -->

---

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
git clone https://github.com/cipher982/zerg.git
cd zerg
cp .env.example .env

# Start the stack
docker compose -f docker/docker-compose.dev.yml --profile dev up -d

# Open http://localhost:30080/timeline
```

### Option 2: Local Development

Requires: Node.js 20+, Python 3.11+, PostgreSQL

```bash
git clone https://github.com/cipher982/zerg.git
cd zerg

# Install dependencies
bun install
cd apps/zerg/backend && uv sync && cd ../../..

# Configure
cp .env.example .env
# Edit .env: set DATABASE_URL to your Postgres

# Start everything
make dev

# Open http://localhost:30080/timeline
```

---

## Features

- **Unified Timeline** — See all your AI coding sessions in one place, sorted by time
- **Multi-Provider Support** — Claude Code, Codex, Gemini, Cursor (more coming)
- **Session Search** — Find conversations by project, content, or date
- **Demo Mode** — Try it instantly with sample sessions, no API key needed
- **Oikos Chat** — Built-in AI assistant that can browse your session history

---

## How It Works

```
Your IDE/CLI                    Zerg
┌──────────────┐               ┌──────────────────────┐
│ Claude Code  │──────────────▶│                      │
│ Codex CLI    │   session     │  Timeline UI         │
│ Gemini CLI   │   JSONL       │  (http://localhost:  │
│ Cursor       │   files       │   30080/timeline)    │
└──────────────┘               └──────────────────────┘
```

Zerg ingests session files from AI coding tools and presents them in a unified web interface. Sessions are indexed by project, provider, and timestamp.

---

## Status

**Alpha** — Works for personal use. Not production-ready.

### What Works
- Timeline view with session listing
- Session detail with full message history
- Demo session loading
- Graceful degradation without API keys

### Coming Soon
- Automatic session watching (currently requires manual import)
- Session search
- Session tagging and organization
- `curl | sh` installer

---

## Architecture

```
apps/
├── zerg/
│   ├── backend/        # FastAPI + session ingestion
│   └── frontend-web/   # React timeline UI
├── runner/             # Remote execution daemon
└── sauron/             # Scheduled jobs

docker/                 # Compose files + nginx
```

- **Backend**: FastAPI + SQLAlchemy
- **Frontend**: React + React Query
- **Database**: PostgreSQL
- **Package Managers**: Bun (JS), uv (Python)

---

## Configuration

Copy `.env.example` to `.env` and configure:

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `OPENAI_API_KEY` | No | Enables Oikos chat (optional) |
| `FERNET_SECRET` | Yes | Encryption key for credentials |
| `AUTH_DISABLED` | Dev only | Set to `1` for local development |

The UI boots and shows Timeline without any API keys. Chat features prompt for configuration when needed.

---

## Contributing

Issues and PRs welcome. This is a personal project so response times vary.

---

## License

ISC

---

<!-- onboarding-contract:start -->
```json
{
  "primary_route": "/timeline",
  "steps": [
    "cp .env.example .env",
    "docker compose -f docker/docker-compose.dev.yml --profile dev up -d --wait",
    "curl -sf --retry 10 --retry-delay 2 http://localhost:30080/health"
  ],
  "cleanup": [
    "docker compose -f docker/docker-compose.dev.yml --profile dev down -v"
  ],
  "cta_buttons": [
    {"label": "Load demo", "selector": "[data-testid='demo-cta']"}
  ]
}
```
<!-- onboarding-contract:end -->
