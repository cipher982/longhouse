import type { WebSocketMessage } from "../lib/useWebSocket";

declare global {
  interface Window {
    __TEST_WORKER_ID__?: string | number;
    __TEST_WORKSPACE_FALLBACK_MS__?: number;
    __testSendMessage?: (message: WebSocketMessage) => void;
  }
}

export {};
