import type { ReactNode } from "react";
import "@testing-library/jest-dom/vitest";
import { vi, beforeAll, afterAll } from "vitest";

// Suppress expected test output
// These are warnings/errors that are deliberately triggered by tests verifying edge case handling
const originalWarn = console.warn;
const originalError = console.error;

beforeAll(() => {
  console.warn = (...args: unknown[]) => {
    const msg = String(args[0] || '');
    // CommisProgress orphan/edge case warnings - tests deliberately trigger these
    if (msg.includes('[CommisProgress]')) return;
    // ConciergeToolStore failure warnings - tests deliberately trigger these
    if (msg.includes('[ConciergeToolStore]')) return;
    originalWarn.apply(console, args);
  };

  console.error = (...args: unknown[]) => {
    const msg = String(args[0] || '');
    // API errors from tests that verify error handling (e.g., IntegrationsPage 500 test)
    if (msg.includes('[API]') && msg.includes('failed with status')) return;
    originalError.apply(console, args);
  };
});

afterAll(() => {
  console.warn = originalWarn;
  console.error = originalError;
});

vi.mock("../lib/auth", () => {
  const noop = async () => {};
  return {
    useAuth: () => ({
      user: {
        id: 1,
        email: "test@local",
        display_name: "Test User",
        is_active: true,
        created_at: new Date(0).toISOString(),
      },
      isAuthenticated: true,
      isLoading: false,
      login: noop,
      logout: noop,
      getToken: () => "test-token",
    }),
    AuthProvider: ({ children }: { children: ReactNode }) => children,
  };
});

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(public url: string) {}

  send() {}

  close() {
    if (this.onclose) {
      this.onclose(new Event('close') as CloseEvent);
    }
  }

  addEventListener(_type: string, _listener: EventListener) {}
  removeEventListener(_type: string, _listener: EventListener) {}
}

// @ts-expect-error – jsdom lacks WebSocket; provide lightweight shim for tests
global.WebSocket = MockWebSocket;

// Mock DOMPurify - jsdom doesn't support all its features
vi.mock("dompurify", () => ({
  default: {
    sanitize: (html: string) => html,
    addHook: () => {},
    removeAllHooks: () => {},
  },
}));
