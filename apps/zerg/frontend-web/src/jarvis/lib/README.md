# Jarvis Library (`src/jarvis/lib/`)

Core services and controllers used by React hooks.

## Current Architecture (December 2025)

The library layer provides stateful services that React hooks consume:

| Module | Purpose | Used By |
|--------|---------|---------|
| `config.ts` | Environment + base URLs | Most modules |
| `event-bus.ts` | Internal event fanout (SSE → UI stores/components) | Chat + progress UI |
| `state-manager.ts` | Chat/session state + assistant status updates | `useJarvisApp`, `useTextChannel` |
| `supervisor-chat-controller.ts` | SSE streaming for `POST /api/jarvis/chat` | `useTextChannel` |
| `conversation-controller.ts` | Streaming text accumulation | Chat rendering |
| `worker-progress-store.ts` | Worker lifecycle/tool progress state | `WorkerProgress` |
| `supervisor-tool-store.ts` | Supervisor tool card state | `ActivityStream` |
| `timeline-logger.ts` | Timeline logging (`?log=timeline`) | Performance/debug |
| `session-handler.ts` | OpenAI Realtime session management (voice I/O) | `useJarvisApp` |

## Design Philosophy

The library layer handles:

- **Network communication** (SSE, WebRTC)
- **Event aggregation** (streaming deltas → complete messages)
- **External SDK integration** (OpenAI Realtime API)

React hooks in `src/hooks/` consume these services and expose React-friendly APIs.

## Notes

Some older migration notes and “deleted files” lists may exist in historical docs; treat `AGENTS.md` as the source of truth for current architecture.
