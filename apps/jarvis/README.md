# Jarvis Voice Agent Platform

Hybrid voice AI platform supporting both **personal** (Jarvis) and **work** (Zeta Athena) contexts with complete separation and security.

## Quick Start

```bash
# Setup (one-time)
make install

# Personal Assistant (default)
make start        # Jarvis with health/location tools
make personal     # Same as above

# Work Assistant  
make zeta         # Zeta Athena with company knowledge/RAG tools
```

Access at **http://localhost:8080** (desktop) or **http://REDACTED_LOCAL_IP:8080** (iPhone)

## Dual Context Architecture

### **Personal Context (Jarvis) 🤖**
- **Tools**: Location (GPS), WHOOP (health data), Obsidian (notes)
- **Theme**: Dark blue-cyan aesthetic
- **Database**: `JarvisVoiceAI-personal`
- **Config**: Committed to repo (`contexts/personal/`)

### **Work Context (Zeta Athena) 🏢**  
- **Tools**: Location (travel), Company Knowledge (RAG), Financial Data, Team Info
- **Theme**: Professional purple aesthetic
- **Database**: `JarvisVoiceAI-zeta` 
- **Config**: Gitignored (`contexts/zeta/`) - **never committed**

## Technology Stack

- **TypeScript**: Full type safety with strict checking
- **OpenAI Agents SDK**: Simplified Realtime API integration
- **Context System**: Runtime switching with isolated configurations
- **IndexedDB**: Conversation persistence per context
- **Vite**: Modern bundling with hot module replacement
- **Progressive Web App**: iPhone installation and offline support
- **Monorepo Packages**: Shared voice engine (`packages/core`) and local data services (`packages/data/local`) consumed by all clients

## Context Management

**Work configs are never committed** to your personal repo:

```bash
apps/web/contexts/
├── personal/          # ✅ Committed (your personal config)
│   ├── config.ts      # Jarvis setup with MCP tools  
│   ├── theme.css      # Dark blue theme
│   └── manifest.json  # Context metadata
├── zeta/              # 🚫 Gitignored (work stays private)
│   ├── config.ts      # Company-specific setup
│   ├── theme.css      # Company branding
│   └── *.template.*   # Templates for setup
└── context-loader.ts  # Runtime switching system
```

## Context Switching

**Environment Variable:**
```bash
VITE_VOICE_CONTEXT=zeta npm run dev    # Work mode
VITE_VOICE_CONTEXT=personal npm run dev # Personal mode (default)
```

**URL Parameter:**
```
http://localhost:8080?context=zeta     # Runtime switching
```

**UI Selector:** Dropdown in the interface for easy switching

## Demo Features

### Personal (Jarvis)
1. **"What's my recovery?"** → WHOOP health data via MCP
2. **"Where am I?"** → GPS location via MCP  
3. **"Show my notes"** → Obsidian search via MCP

### Work (Zeta Athena)
1. **"What were our Q3 results?"** → Financial data via RAG
2. **"Where am I?"** → Location for work travel
3. **"Who's on the engineering team?"** → Team info via RAG
4. **"What's our remote work policy?"** → Company docs via RAG

## Configuration

**Environment Variables:**
```bash
# server/.env (required)
OPENAI_API_KEY=sk-your-key-here

# Optional context selection
VITE_VOICE_CONTEXT=personal|zeta
```

**Work Context Setup:**
```bash
# Copy templates to create work config
cp apps/web/contexts/zeta/config.template.ts apps/web/contexts/zeta/config.ts
cp apps/web/contexts/zeta/theme.template.css apps/web/contexts/zeta/theme.css

# Customize for your company (these files stay gitignored)
```

## Security Model

- **Server-side API keys**: OpenAI credentials never exposed to client
- **Ephemeral tokens**: Short-lived client secrets for WebRTC connection  
- **Context isolation**: Personal and work data completely separated
- **Private configs**: Work context configurations never committed to repo
- **MCP bridge**: Personal API access controlled through server endpoints

## Development Commands

```bash
make help       # Show all available commands
make install    # Install dependencies across all packages
make start      # Jarvis personal context (default)
make zeta       # Zeta Athena work context
make stop       # Stop all running servers
make test       # Run comprehensive test suite
```

## Multi-Platform Deployment

- **Progressive Web App**: Install on iPhone, works offline
- **Native macOS**: Electron wrapper with global hotkey (⌘+J)
- **Cross-platform**: Same codebase, different deployment targets
- **Production Ready**: HTTPS deployment ready, secure token management

Built for production deployment with complete work/personal separation and enterprise-grade security.
