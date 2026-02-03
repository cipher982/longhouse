# Longhouse

Never lose an AI coding conversation.

Search across Claude, Codex, Cursor, Gemini. Resume from anywhere.

## Quick Start

```bash
pip install longhouse
longhouse serve
# Open http://localhost:8080
```

## Features

- **Timeline**: Searchable archive of all your AI coding sessions
- **Search**: FTS5-powered instant discovery across all sessions
- **Resume**: Continue any session from any device

## Install Options

### 1. pip install (Recommended)
```bash
pip install longhouse
longhouse serve --demo  # Try with sample data
```

### 2. Docker
```bash
docker compose up
```

### 3. From source
```bash
git clone https://github.com/cipher982/longhouse
cd longhouse && make dev
```

## Configuration

Set `LONGHOUSE_PASSWORD` for remote access authentication.

## Commands

```bash
longhouse serve      # Start the server
longhouse serve --demo   # Start with sample data
longhouse connect    # Sync Claude Code sessions (continuous)
longhouse ship       # One-time sync
longhouse status     # Show configuration
longhouse onboard    # Re-run setup wizard
```

## Documentation

See [docs/](docs/) for detailed guides.

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
