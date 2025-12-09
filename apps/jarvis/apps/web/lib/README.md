# Legacy Controllers Directory

⚠️ **DEPRECATED** - This directory contains the original vanilla TypeScript controllers.

## Status

- **Deprecated**: December 2024
- **Removal Target**: March 1, 2025
- **Current Use**: Only via `useRealtimeSession` bridge hook when `VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true`

## Migration Path

These controllers are being replaced by React hooks in `src/hooks/`:

| Legacy Controller            | New Hook                     | Status               |
| ---------------------------- | ---------------------------- | -------------------- |
| `app-controller.ts`          | `useJarvisClient.ts`         | ✅ Basic replacement |
| `voice-controller.ts`        | `useVoice.ts`                | ✅ Basic replacement |
| `text-channel-controller.ts` | `useTextChannel.ts`          | ✅ Basic replacement |
| `audio-controller.ts`        | `useVoice.ts` (integrated)   | ✅ Basic replacement |
| `conversation-controller.ts` | Context + hooks              | ✅ Basic replacement |
| `state-manager.ts`           | `src/context/AppContext.tsx` | ✅ Replaced          |
| `session-handler.ts`         | `useRealtimeSession.ts`      | 🔄 Bridge mode       |

## DO NOT

- ❌ Add new features to these files
- ❌ Import these from new React components
- ❌ Fix bugs here (fix in React hooks instead)
- ❌ Refactor this code

## Allowed Usage

- ✅ `useRealtimeSession` hook can import these (bridge mode)
- ✅ Existing tests can import these until migration complete

## Deletion Checklist

Before removing this directory (target: March 2025):

- [ ] Complete realtime session implementation in useVoice hook
- [ ] Port remaining business logic to React hooks
- [ ] Remove `VITE_JARVIS_ENABLE_REALTIME_BRIDGE` flag
- [ ] Delete `useRealtimeSession` bridge hook
- [ ] Delete this directory
- [ ] Delete `main.ts` legacy entry point
- [ ] Delete `contexts/` directory
