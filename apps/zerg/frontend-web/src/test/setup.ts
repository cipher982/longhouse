import "@testing-library/jest-dom/vitest";

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
