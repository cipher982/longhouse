import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectLoopInboxStream } from "../oikos";

// This covers the browser EventSource client contract only.
// Real Home Screen/PWA push delivery and OS notification-click behavior remain separate device-level canaries.

type EventListener = (event: MessageEvent) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  options: EventSourceInit | undefined;
  listeners = new Map<string, EventListener[]>();
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();

  constructor(url: string, options?: EventSourceInit) {
    this.url = url;
    this.options = options;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type: string, payload: unknown) {
    const event = { data: JSON.stringify(payload) } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

describe("Loop inbox stream", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("connects with cookie auth and parses loop inbox snapshots", () => {
    const onConnected = vi.fn();
    const onHeartbeat = vi.fn();
    const onSnapshot = vi.fn();

    const disconnect = connectLoopInboxStream({
      onConnected,
      onHeartbeat,
      onSnapshot,
    });

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe("/api/oikos/loop-inbox/stream");
    expect(MockEventSource.instances[0].options).toEqual({ withCredentials: true });

    MockEventSource.instances[0].emit("connected", { timestamp: "2026-03-22T20:15:00Z" });
    MockEventSource.instances[0].emit("heartbeat", { timestamp: "2026-03-22T20:15:10Z" });
    MockEventSource.instances[0].emit("inbox_snapshot", {
      items: [
        {
          card_id: 42,
          session_id: "sess-1",
          title: "Hiring managed",
          project: "hiring",
          machine: "cinder",
          provider: "claude",
          home_label: "On this Mac",
          loop_mode: "assist",
          decision: "ask_user",
          execution_state: "awaiting_user_approval",
          summary: "Run the pending targeted tests.",
          recommended_action: "continue_session",
          follow_up_prompt: "Run the pending targeted tests.",
          blocked_reasons: [],
          last_turn_at: "2026-03-22T20:15:11Z",
          card_state: "active",
          card_state_reason: null,
          superseded_by_card_id: null,
          requires_attention: true,
        },
      ],
    });

    expect(onConnected).toHaveBeenCalledTimes(1);
    expect(onHeartbeat).toHaveBeenCalledWith("2026-03-22T20:15:10Z");
    expect(onSnapshot).toHaveBeenCalledWith({
      items: [
        expect.objectContaining({
          cardId: 42,
          sessionId: "sess-1",
          homeLabel: "On this Mac",
        }),
      ],
    });

    disconnect();
    expect(MockEventSource.instances[0].close).toHaveBeenCalledTimes(1);
  });
});
