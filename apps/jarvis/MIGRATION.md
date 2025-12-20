# Jarvis React Migration

## Status: Complete (December 2025)

The React migration is **complete**. Jarvis now runs as a pure React application.

## Architecture

### Current (React-only)

- **Entry**: `src/main.tsx` - React root with StrictMode
- **State**: `src/context/AppContext.tsx` - React Context + useReducer
- **Logic**: `src/hooks/*.ts` - Custom hooks for business logic
- **UI**: `src/components/*.tsx` - Declarative React components

### Key Hooks

| Hook              | Purpose                                   |
| ----------------- | ----------------------------------------- |
| `useJarvisApp`    | Initialization, connection, voice control |
| `useVoice`        | OpenAI Realtime API voice I/O             |
| `useTextChannel`  | Text messaging via SSE                    |
| `usePreferences`  | Model selection and user preferences      |
| `useJarvisClient` | Backend API client                        |

## What Was Removed

The following legacy code was deleted:

- **Bridge mode** (`VITE_JARVIS_ENABLE_REALTIME_BRIDGE`) - No longer needed
- **useRealtimeSession hook** - Replaced by `useJarvisApp`
- **app-controller.ts** - Logic moved to hooks
- **Legacy tests** - Rewritten for new architecture

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

## See Also

- [LEGACY_CODE_REMOVAL.md](./LEGACY_CODE_REMOVAL.md) - Detailed migration tracking
- [Main AGENTS.md](../../AGENTS.md) - Project overview
