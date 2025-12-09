# Jarvis React Migration

## Overview

Jarvis has been migrated from vanilla TypeScript with manual DOM manipulation to React with TypeScript. This migration provides:

- **Declarative UI**: React components replace imperative DOM updates
- **Type Safety**: Full TypeScript coverage with React JSX
- **State Management**: React Context + useReducer instead of custom StateManager
- **Reusable Components**: Shared components with Zerg frontend
- **Modern Tooling**: Vite build, Hot Module Replacement, PWA support

## Feature Flag: ENABLE_REALTIME_BRIDGE

The migration includes a feature flag that controls integration with legacy code:

```bash
# .env or .env.local
VITE_JARVIS_ENABLE_REALTIME_BRIDGE=false  # Default: Standalone React mode
# VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true   # Enable legacy controller bridge
```

### Modes

| Mode                           | Description                                            | Use Case                                         |
| ------------------------------ | ------------------------------------------------------ | ------------------------------------------------ |
| **Standalone React** (default) | Pure React implementation with simulated responses     | Development, testing, UI work                    |
| **Legacy Bridge**              | Connects to old controllers for full realtime features | Production (temporary), full voice/audio testing |

### Status

- **Standalone mode** (default, `VITE_JARVIS_ENABLE_REALTIME_BRIDGE=false`): Pure React with simulated responses for UI development
- **Bridge mode** (`VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true`): Full functionality via legacy controllers

**Recommendation**: Use bridge mode (`VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true`) for:

- Production deployments
- Full voice/audio testing
- Backend integration testing

### Migration Progress

The legacy bridge is actively maintained while React hooks are being enhanced.
Legacy code removal will happen after React hooks achieve feature parity with:

- Text message sending (via realtime session)
- Voice PTT and hands-free modes
- Conversation history management
- Context/session handling

### Warning

When `ENABLE_REALTIME_BRIDGE=true`, the console will show a deprecation notice.
This is informational - bridge mode is fully functional and recommended for production.

## Migration Status

✅ **Complete:**

- React app scaffold
- UI components (Sidebar, Header, VoiceControls, ChatContainer, TextInput)
- React Context state management
- Custom hooks (useVoice, useTextChannel, useJarvisClient, useRealtimeSession)
- PWA service worker with offline support
- Feature flag policy

🔄 **In Progress:**

- Legacy code cleanup (target: Jan 2025)
- Integration tests for voice/audio
- Full realtime session implementation in React

## Development

```bash
# Start dev server
cd apps/jarvis/apps/web
bun run dev

# Type check
bun run type-check

# Run tests
bun run test

# Build for production
bun run build
```

## Architecture

### Before (Legacy)

- **Entry**: `main.ts` - Manual DOM initialization
- **State**: `lib/state-manager.ts` - Custom pub/sub singleton
- **Controllers**: `lib/*-controller.ts` - Class-based with manual listeners
- **UI**: `index.html` + imperative `.innerHTML` updates

### After (React)

- **Entry**: `src/main.tsx` - React root with StrictMode
- **State**: `src/context/AppContext.tsx` - React Context + useReducer
- **Logic**: `src/hooks/*.ts` - Custom hooks for business logic
- **UI**: `src/components/*.tsx` - Declarative React components

## Files

### React App (New)

```
src/
├── main.tsx                 # React entry point
├── App.tsx                  # Main app component
├── components/              # UI components
│   ├── Sidebar.tsx
│   ├── Header.tsx
│   ├── VoiceControls.tsx
│   ├── ChatContainer.tsx
│   ├── TextInput.tsx
│   └── OfflineBanner.tsx
├── context/                 # State management
│   ├── AppContext.tsx
│   └── types.ts
└── hooks/                   # Business logic
    ├── useVoice.ts
    ├── useTextChannel.ts
    ├── useJarvisClient.ts
    └── useRealtimeSession.ts  # Bridge to legacy (temporary)
```

### Legacy (To Remove)

```
main.ts                      # OLD entry point - marked for removal
lib/                         # OLD controllers - marked for removal
contexts/                    # OLD context loader - marked for removal
```

## See Also

- [Main AGENTS.md](../../AGENTS.md) - Project overview
- [Root README](../../../../README.md) - Platform documentation
