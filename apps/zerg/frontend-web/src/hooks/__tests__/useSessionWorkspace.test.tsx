import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionWorkspace } from "../useSessionWorkspace";

const agentSessionMocks = vi.hoisted(() => ({
  useAgentSession: vi.fn(),
  useAgentSessionThread: vi.fn(),
  useAgentSessionProjectionInfinite: vi.fn(),
}));

vi.mock("../useAgentSessions", () => agentSessionMocks);

const baseSession = {
  id: "session-1",
  thread_head_session_id: "session-1",
  provider: "claude",
  project: "session-workspace-test",
} as const;

function makeEvents(count: number) {
  const startedAt = Date.parse("2026-03-14T12:00:00.000Z");
  return Array.from({ length: count }, (_, index) => ({
    id: index + 1,
    role: index % 2 === 0 ? "user" : "assistant",
    timestamp: new Date(startedAt + index * 1_000).toISOString(),
    content_text: `Session event ${index + 1}`,
    tool_name: null,
    tool_call_id: null,
    tool_input_json: null,
    tool_output_text: null,
    in_active_context: true,
  }));
}

function seedHookMocks(eventCount: number = 80) {
  const events = makeEvents(eventCount);
  agentSessionMocks.useAgentSession.mockReturnValue({
    data: baseSession,
    isLoading: false,
    error: null,
  });
  agentSessionMocks.useAgentSessionThread.mockReturnValue({
    data: {
      sessions: [baseSession],
      head_session_id: baseSession.id,
    },
  });
  agentSessionMocks.useAgentSessionProjectionInfinite.mockReturnValue({
    data: {
      pages: [
        {
          items: events.map((event) => ({
            kind: "event",
            session_id: baseSession.id,
            timestamp: event.timestamp,
            event,
          })),
          total: events.length,
          abandoned_events: 0,
        },
      ],
    },
    isLoading: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  });
}

function makeScrollableTimelineList({
  clientHeight,
  scrollHeight,
}: {
  clientHeight: number;
  scrollHeight: number;
}) {
  const element = document.createElement("div");
  let currentClientHeight = clientHeight;
  let currentScrollHeight = scrollHeight;
  let currentScrollTop = 0;

  Object.defineProperty(element, "clientHeight", {
    configurable: true,
    get: () => currentClientHeight,
  });
  Object.defineProperty(element, "scrollHeight", {
    configurable: true,
    get: () => currentScrollHeight,
  });
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => currentScrollTop,
    set: (value: number) => {
      const maxScrollTop = Math.max(0, currentScrollHeight - currentClientHeight);
      currentScrollTop = Math.max(0, Math.min(value, maxScrollTop));
    },
  });

  return {
    element,
    get scrollTop() {
      return currentScrollTop;
    },
    setLayout(nextLayout: { clientHeight?: number; scrollHeight?: number }) {
      if (typeof nextLayout.clientHeight === "number") {
        currentClientHeight = nextLayout.clientHeight;
      }
      if (typeof nextLayout.scrollHeight === "number") {
        currentScrollHeight = nextLayout.scrollHeight;
      }
    },
  };
}

describe("useSessionWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    document.body.innerHTML = "";
    seedHookMocks();
  });

  it("scrolls the timeline list to the latest context when the container is already scrollable", async () => {
    const { result } = renderHook(() => useSessionWorkspace(baseSession.id));
    const list = makeScrollableTimelineList({
      clientHeight: 320,
      scrollHeight: 1800,
    });

    document.body.appendChild(list.element);

    act(() => {
      result.current.registerTimelineList(list.element);
    });

    await waitFor(() => {
      expect(list.scrollTop).toBeGreaterThan(0);
    });
  });

  it("retries auto-scroll until the timeline list becomes scrollable", async () => {
    const queuedFrames = new Map<number, FrameRequestCallback>();
    let nextFrameId = 1;
    const requestAnimationFrameSpy = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback: FrameRequestCallback) => {
        const frameId = nextFrameId++;
        queuedFrames.set(frameId, callback);
        return frameId;
      });
    const cancelAnimationFrameSpy = vi
      .spyOn(window, "cancelAnimationFrame")
      .mockImplementation((frameId: number) => {
        queuedFrames.delete(frameId);
      });

    try {
      const { result } = renderHook(() => useSessionWorkspace(baseSession.id));
      const list = makeScrollableTimelineList({
        clientHeight: 320,
        scrollHeight: 320,
      });

      document.body.appendChild(list.element);

      act(() => {
        result.current.registerTimelineList(list.element);
      });

      expect(list.scrollTop).toBe(0);
      expect(queuedFrames.size).toBeGreaterThan(0);

      list.setLayout({ scrollHeight: 1800 });

      act(() => {
        const [frameId, nextFrame] = queuedFrames.entries().next().value ?? [];
        if (!nextFrame || typeof frameId !== "number") {
          throw new Error("Expected a queued animation frame callback");
        }
        queuedFrames.delete(frameId);
        nextFrame(16);
      });

      await waitFor(() => {
        expect(list.scrollTop).toBeGreaterThan(0);
      });
    } finally {
      requestAnimationFrameSpy.mockRestore();
      cancelAnimationFrameSpy.mockRestore();
    }
  });

  it("builds a stitched seam row for cloud child sessions", () => {
    const parentSession = {
      ...baseSession,
      id: "session-parent",
      thread_head_session_id: "session-child",
      continuation_kind: "local",
      continued_from_session_id: null,
      origin_label: "Local",
      started_at: "2026-03-19T16:40:00Z",
    };
    const childSession = {
      ...baseSession,
      id: "session-child",
      thread_head_session_id: "session-child",
      continuation_kind: "cloud",
      continued_from_session_id: "session-parent",
      origin_label: "Cloud",
      started_at: "2026-03-19T16:45:00Z",
    };

    agentSessionMocks.useAgentSession.mockReturnValue({
      data: childSession,
      isLoading: false,
      error: null,
    });
    agentSessionMocks.useAgentSessionThread.mockReturnValue({
      data: {
        sessions: [parentSession, childSession],
        head_session_id: childSession.id,
      },
    });
    agentSessionMocks.useAgentSessionProjectionInfinite.mockReturnValue({
      data: {
        pages: [
          {
            items: [
              {
                kind: "event",
                session_id: parentSession.id,
                timestamp: "2026-03-19T16:40:00Z",
                event: makeEvents(1)[0],
              },
              {
                kind: "seam",
                session_id: childSession.id,
                timestamp: "2026-03-19T16:45:00Z",
                continued_from_session_id: parentSession.id,
                continuation_kind: "cloud",
                origin_label: "Cloud",
                parent_origin_label: "Local",
                parent_continuation_kind: "local",
                branched_from_event_id: 12,
              },
            ],
            total: 2,
            abandoned_events: 0,
          },
        ],
      },
      isLoading: false,
      error: null,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    });

    const { result } = renderHook(() => useSessionWorkspace(childSession.id));

    expect(result.current.items[1]).toEqual({
      kind: "seam",
      seam: {
        key: "seam:session-child:2026-03-19T16:45:00Z",
        sessionId: "session-child",
        label: "Cloud continuation begins",
        description: "Synced Local history above. Cloud-native messages below.",
        timestamp: "2026-03-19T16:45:00Z",
      },
    });
  });

  it("debounces search query updates through the shared debounce hook", () => {
    vi.useFakeTimers();

    try {
      const { result } = renderHook(() => useSessionWorkspace(baseSession.id));

      act(() => {
        result.current.setSearchQuery("Session event 12");
      });

      expect(result.current.searchQuery).toBe("Session event 12");
      expect(result.current.debouncedSearch).toBe("");

      act(() => {
        vi.advanceTimersByTime(300);
      });

      expect(result.current.debouncedSearch).toBe("Session event 12");
    } finally {
      vi.useRealTimers();
    }
  });
});
