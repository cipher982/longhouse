import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "react-hot-toast";
import { useWebSocket } from "../useWebSocket";

vi.mock("../config", () => ({
  getWebSocketConfig: () => ({
    baseUrl: "http://localhost:47300",
    reconnectInterval: 1000,
    maxReconnectAttempts: 3,
  }),
}));

vi.mock("react-hot-toast", () => ({
  toast: {
    error: vi.fn(),
  },
}));

type WebSocketListenerMap = {
  close: Set<EventListener>;
  error: Set<EventListener>;
  message: Set<EventListener>;
  open: Set<EventListener>;
};

const originalWebSocket = global.WebSocket;
const mockSockets: MockWebSocket[] = [];

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  public onopen: ((event: Event) => void) | null = null;
  public onclose: ((event: CloseEvent) => void) | null = null;
  public onmessage: ((event: MessageEvent) => void) | null = null;
  public onerror: ((event: Event) => void) | null = null;
  public readyState = MockWebSocket.OPEN;
  public sent: string[] = [];

  private listeners: WebSocketListenerMap = {
    close: new Set(),
    error: new Set(),
    message: new Set(),
    open: new Set(),
  };

  constructor(public url: string) {
    mockSockets.push(this);
  }

  addEventListener(type: keyof WebSocketListenerMap, listener: EventListener) {
    this.listeners[type].add(listener);
  }

  removeEventListener(type: keyof WebSocketListenerMap, listener: EventListener) {
    this.listeners[type].delete(listener);
  }

  send(payload: string) {
    this.sent.push(payload);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.emit("close", new Event("close"));
  }

  emit(type: keyof WebSocketListenerMap, event: Event) {
    this.listeners[type].forEach((listener) => listener(event));

    if (type === "open") {
      this.onopen?.(event);
    } else if (type === "close") {
      this.onclose?.(event as CloseEvent);
    } else if (type === "message") {
      this.onmessage?.(event as MessageEvent);
    } else if (type === "error") {
      this.onerror?.(event);
    }
  }

  emitMessage(payload: unknown) {
    this.emit("message", new MessageEvent("message", { data: JSON.stringify(payload) }));
  }
}

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useWebSocket", () => {
  beforeEach(() => {
    mockSockets.length = 0;
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.stubGlobal("WebSocket", originalWebSocket);
  });

  it("uses the latest handlers and invalidation keys without reconnecting", async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const invalidateQueriesSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = createWrapper(queryClient);
    const firstHandler = vi.fn();
    const secondHandler = vi.fn();

    const { rerender } = renderHook(
      ({ invalidateQueries, onMessage }) =>
        useWebSocket(true, {
          invalidateQueries,
          onMessage,
        }),
      {
        initialProps: {
          invalidateQueries: [["initial-query"]],
          onMessage: firstHandler,
        },
        wrapper,
      },
    );

    await waitFor(() => {
      expect(mockSockets).toHaveLength(1);
    });

    const [socket] = mockSockets;
    act(() => {
      socket.emit("open", new Event("open"));
    });

    rerender({
      invalidateQueries: [["updated-query"]],
      onMessage: secondHandler,
    });

    expect(mockSockets).toHaveLength(1);

    act(() => {
      socket.emitMessage({ type: "automation_state", data: { status: "running" } });
    });

    expect(firstHandler).not.toHaveBeenCalled();
    expect(secondHandler).toHaveBeenCalledWith({
      type: "automation_state",
      data: { status: "running" },
    });
    expect(invalidateQueriesSpy).toHaveBeenCalledWith({ queryKey: ["updated-query"] });
  });

  it("disconnects the active socket once on unmount", async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const wrapper = createWrapper(queryClient);

    const { unmount } = renderHook(() => useWebSocket(true), { wrapper });

    await waitFor(() => {
      expect(mockSockets).toHaveLength(1);
    });

    const [socket] = mockSockets;
    const closeSpy = vi.spyOn(socket, "close");

    unmount();

    expect(closeSpy).toHaveBeenCalledTimes(1);
  });

  it("reports the first real error after an intentional reconnect", async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const wrapper = createWrapper(queryClient);
    const onError = vi.fn();

    const { result } = renderHook(
      () =>
        useWebSocket(true, {
          onError,
        }),
      { wrapper },
    );

    await waitFor(() => {
      expect(mockSockets).toHaveLength(1);
    });

    act(() => {
      mockSockets[0].emit("open", new Event("open"));
      result.current.reconnect();
    });

    await waitFor(() => {
      expect(mockSockets).toHaveLength(2);
    });

    act(() => {
      mockSockets[1].emit("error", new Event("error"));
    });

    await waitFor(() => {
      expect(onError).toHaveBeenCalledTimes(1);
      expect(result.current.connectionStatus).toBe("error");
      expect(toast.error).toHaveBeenCalledWith(
        "WebSocket connection failed. Real-time features disabled.",
        { duration: 5000 },
      );
    });
  });
});
