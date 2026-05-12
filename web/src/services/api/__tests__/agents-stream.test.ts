import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectTimelineSessionsStream } from "../agents";

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

describe("Timeline session stream", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
    (window as typeof window & { __TEST_COMMIS_ID__?: string }).__TEST_COMMIS_ID__ = "17";
  });

  afterEach(() => {
    delete (window as typeof window & { __TEST_COMMIS_ID__?: string }).__TEST_COMMIS_ID__;
    vi.unstubAllGlobals();
  });

  it("connects with cookie auth and routes session upsert events", () => {
    const onSessionUpsert = vi.fn();
    const onTimelineStreamEvent = vi.fn();
    window.addEventListener("longhouse:timeline-stream", onTimelineStreamEvent);
    const disconnect = connectTimelineSessionsStream(
      {
        project: "zerg",
        days_back: 14,
        limit: 50,
        hide_autonomous: false,
      },
      { onSessionUpsert },
      { skipInitialReplay: true },
    );

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toContain("/api/timeline/sessions/stream");
    expect(MockEventSource.instances[0].url).toContain("project=zerg");
    expect(MockEventSource.instances[0].url).toContain("days_back=14");
    expect(MockEventSource.instances[0].url).toContain("limit=50");
    expect(MockEventSource.instances[0].url).toContain("hide_autonomous=false");
    expect(MockEventSource.instances[0].url).toContain("skip_initial_replay=true");
    expect(MockEventSource.instances[0].url).toContain("commis=17");
    expect(MockEventSource.instances[0].options).toEqual({ withCredentials: true });

    MockEventSource.instances[0].emit("connected", { message: "ok" });

    MockEventSource.instances[0].emit("session_upsert", {
      session: {
        thread_id: "session-1",
        head: { id: "session-1" },
        id: "session-1",
        provider: "claude",
        project: "zerg",
        device_id: "device-1",
        environment: "laptop",
        cwd: "/tmp/zerg",
        git_repo: null,
        git_branch: "main",
        started_at: "2026-03-21T12:00:00Z",
        ended_at: null,
        last_activity_at: "2026-03-21T12:04:00Z",
        timeline_anchor_at: "2026-03-21T12:04:00Z",
        user_messages: 2,
        assistant_messages: 2,
        tool_calls: 1,
        summary: "runtime",
        summary_title: "runtime",
        first_user_message: "hello",
        thread_root_session_id: "session-1",
        thread_head_session_id: "session-1",
        thread_continuation_count: 1,
        continued_from_session_id: null,
        continuation_kind: null,
        origin_label: null,
        home_label: null,
        branched_from_event_id: null,
        is_writable_head: true,
        loop_mode: "assist",
      },
      total: 1,
      has_real_sessions: true,
    });

    expect(onSessionUpsert).toHaveBeenCalledTimes(1);
    expect(onSessionUpsert).toHaveBeenCalledWith(
      expect.objectContaining({
        total: 1,
        has_real_sessions: true,
      }),
    );
    const streamEventDetails = onTimelineStreamEvent.mock.calls.map(([event]) => (event as CustomEvent).detail);
    expect(streamEventDetails).toContainEqual(expect.objectContaining({ kind: "connected" }));
    expect(streamEventDetails).toContainEqual(
      expect.objectContaining({ kind: "session_upsert", session_id: "session-1" }),
    );

    disconnect();
    window.removeEventListener("longhouse:timeline-stream", onTimelineStreamEvent);
    expect(MockEventSource.instances[0].close).toHaveBeenCalledTimes(1);
  });
});
