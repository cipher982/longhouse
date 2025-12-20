# Jarvis Web Library

Core services and controllers used by React hooks.

## Current Architecture (December 2025)

The library layer provides stateful services that React hooks consume:

| Module                          | Purpose                              | Used By                |
| ------------------------------- | ------------------------------------ | ---------------------- |
| `state-manager.ts`              | Event bus for streaming + state sync | useJarvisApp, useVoice |
| `supervisor-chat-controller.ts` | SSE streaming to Zerg backend        | useTextChannel         |
| `conversation-controller.ts`    | Streaming text accumulation          | useJarvisApp           |
| `session-handler.ts`            | OpenAI Realtime session management   | useVoice               |
| `supervisor-progress.ts`        | Worker progress UI rendering         | App.tsx                |
| `config.ts`                     | Environment configuration            | All modules            |

## Design Philosophy

The library layer handles:

- **Network communication** (SSE, WebRTC)
- **Event aggregation** (streaming deltas → complete messages)
- **External SDK integration** (OpenAI Realtime API)

React hooks in `src/hooks/` consume these services and expose React-friendly APIs.

## Deleted Files (December 2025)

The following were removed during the React migration:

- `app-controller.ts` - Replaced by `useJarvisApp` hook
- `voice-controller.ts` - Merged into `useVoice` hook
- `text-channel-controller.ts` - Replaced by `useTextChannel` hook
- `event-bus.ts` - No longer needed
- `task-inbox.ts` - Feature removed
- `test-helpers.ts` - Tests updated

See `LEGACY_CODE_REMOVAL.md` for full migration details.
