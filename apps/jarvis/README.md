# Jarvis Voice Agent Platform

Personal voice AI assistant with real-time speech interaction and agent orchestration.

## Quick Start

### Option A: Unified Docker Compose (Recommended)
```bash
# Start all services together (Jarvis + Zerg) from repo root
cd ../../../
./start-unified-dev.sh

# Access Jarvis at http://localhost:30080
```

### Option B: Standalone
```bash
# Install dependencies
make install

# Start Jarvis (default personal context)
make start

# Or start directly
cd apps/web && npm run dev
```

Access at **http://localhost:8080** (standalone) or **http://localhost:30080** (unified)

## Features

### Personal Context (Jarvis) 🤖
- **Voice Interface**: Real-time speech with OpenAI Realtime API
- **Text Input**: Type commands when voice isn't convenient
- **Task Inbox**: See background agent runs in real-time
- **Tools**: Location (GPS), WHOOP (health data), Obsidian (notes)
- **Theme**: Dark blue-cyan aesthetic
- **PWA**: Install on iPhone/Android, works offline

## Technology Stack

- **TypeScript**: Full type safety with strict checking
- **OpenAI Realtime API**: Speech-to-speech conversation
- **IndexedDB**: Conversation persistence
- **Vite**: Modern bundling with hot module replacement
- **Progressive Web App**: iPhone installation and offline support
- **Monorepo**: Shared voice engine and local data services

## Voice Commands

Try these voice commands:

1. **"What's my recovery?"** → WHOOP health data via MCP
2. **"Where am I?"** → GPS location via MCP
3. **"Show my notes"** → Obsidian search via MCP
4. **"Run my morning digest"** → Trigger Zerg agent
5. **"Quick status"** → Get current time, weather, next event

## Configuration

**Environment Variables:**
```bash
# Required
OPENAI_API_KEY=sk-your-key-here

# Optional (Unified Setup - uses proxy)
VITE_ZERG_API_URL=/api/zerg
VITE_JARVIS_DEVICE_SECRET=your-secret

# Optional (Standalone Setup - direct connection)
# VITE_ZERG_API_URL=http://localhost:47300
```

**Task Inbox Setup:**

**Unified Docker Compose:**
```bash
# 1. Start all services
cd ../../../../
./start-unified-dev.sh

# 2. Seed agents (in another terminal)
docker exec zerg-backend-1 uv run python scripts/seed_jarvis_agents.py
```

**Traditional Setup:**
```bash
# 1. Start Zerg backend
cd ../../../ && make zerg-up

# 2. Seed agents (in another terminal)
cd apps/zerg/backend && uv run python scripts/seed_jarvis_agents.py

# 3. Configure device secret in .env
VITE_JARVIS_DEVICE_SECRET=your-secret-here
VITE_ZERG_API_URL=http://localhost:47300
```

**Note**: In unified mode, API calls go through Nginx proxy at `/api/zerg/*` instead of direct connection.

## Development Commands

```bash
make help       # Show all available commands
make install    # Install dependencies across all packages
make start      # Start Jarvis development server
make stop       # Stop all running servers
make test       # Run test suite
make native     # Build Electron desktop app
```

## Agent Integration

Jarvis integrates with Zerg backend for agent orchestration:

- **Voice Dispatch**: "Run my morning digest" → triggers Zerg agent
- **Real-time Updates**: See agent progress in Task Inbox sidebar
- **Scheduled Agents**: 7 AM daily digest, 8 PM health check, etc.
- **SSE Streaming**: Live updates via Server-Sent Events

## Multi-Platform

- **Progressive Web App**: Install on iPhone, works offline
- **Native macOS**: Electron wrapper with global hotkey (⌘+J)
- **Cross-platform**: Same codebase, different deployment targets
- **Production Ready**: HTTPS deployment ready, secure token management
