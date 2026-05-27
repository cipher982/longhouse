import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionWorkspace } from "../useSessionWorkspace";

const agentSessionMocks = vi.hoisted(() => ({
  useAgentSessionWorkspace: vi.fn(),
  useAgentSessionProjectionInfinite: vi.fn(),
  useAgentSessionTurns: vi.fn(),
}));
const visibilityMocks = vi.hoisted(() => ({
  useDocumentVisible: vi.fn(),
}));
const streamMocks = vi.hoisted(() => ({
  connectSessionWorkspaceStream: vi.fn(() => vi.fn()),
}));
const queryClientMocks = vi.hoisted(() => ({
  invalidateQueries: vi.fn(),
  setQueriesData: vi.fn(),
  getQueryData: vi.fn(() => undefined),
}));
const renderBeaconMocks = vi.hoisted(() => ({
  emitRenderBeacon: vi.fn(),
  recordServerClockSkew: vi.fn(),
}));

vi.mock("../useAgentSessions", () => agentSessionMocks);
vi.mock("../useDocumentVisible", () => visibilityMocks);
vi.mock("../../services/api/agents", () => streamMocks);
vi.mock("../../lib/renderBeacon", () => renderBeaconMocks);
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => queryClientMocks,
}));

const baseSession = {
  id: "session-1",
  thread_head_session_id: "session-1",
  provider: "claude",
  project: "session-workspace-test",
  runtime_display: {
    truth_tier: "none",
    signal_tier: "none",
    state: null,
    tone: "inactive",
    headline: "Inactive",
    detail: null,
    phase_label: "Inactive",
    compact_tool_label: null,
    is_live: false,
    is_executing: false,
    needs_attention: false,
    is_idle: true,
    is_stalled: false,
    is_managed_local_truth: false,
    has_signal: false,
    control_path: "unmanaged",
    activity_recency: "stale",
    lifecycle: "open",
    host_state: null,
    terminal_reason: null,
  },
};

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

function seedHookMocks(eventCount: number = 80, sessionOverrides: Record<string, unknown> = {}) {
  const events = makeEvents(eventCount);
  const session = { ...baseSession, ...sessionOverrides };
  agentSessionMocks.useAgentSessionWorkspace.mockReturnValue({
    data: {
      session,
      thread: {
        sessions: [session],
        head_session_id: baseSession.id,
        root_session_id: baseSession.id,
      },
      projection: {
        root_session_id: baseSession.id,
        focus_session_id: baseSession.id,
        head_session_id: baseSession.id,
        path_session_ids: [baseSession.id],
        items: events.map((event) => ({
          kind: "event",
          session_id: baseSession.id,
          timestamp: event.timestamp,
          event,
        })),
        total: events.length,
        abandoned_events: 0,
      },
    },
    isLoading: false,
    error: null,
  });
  agentSessionMocks.useAgentSessionTurns.mockReturnValue({
    data: {
      turns: [],
      total: 0,
    },
    isLoading: false,
    error: null,
  });
  agentSessionMocks.useAgentSessionProjectionInfinite.mockReturnValue({
    data: {
      pages: [
        {
          page_offset: 0,
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
    fetchPreviousPage: vi.fn(),
    hasPreviousPage: false,
    isFetchingPreviousPage: false,
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
    visibilityMocks.useDocumentVisible.mockReturnValue(true);
    seedHookMocks();
  });

  it("renders a fresh server transcript preview as a synthetic assistant event", () => {
    seedHookMocks(1, {
      transcript_preview: {
        event_id: 99,
        text: "Live preview text before durable transcript arrives",
        event_origin: "live_provisional",
        timestamp: "2026-03-14T12:00:05.000Z",
        is_provisional: true,
        is_complete: false,
        content_cursor: "cursor-99",
        is_stale: false,
        stale_reason: null,
      },
    });

    const { result } = renderHook(() => useSessionWorkspace(baseSession.id));

    expect(result.current.events.map((event) => event.content_text)).toContain(
      "Live preview text before durable transcript arrives",
    );
    expect(result.current.items.at(-1)).toMatchObject({
      kind: "message",
      event: {
        id: -99,
        role: "assistant",
      },
    });
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

  it("keeps polling visible, open sessions to refresh workspace runtime metadata", () => {
    renderHook(() => useSessionWorkspace(baseSession.id));

    expect(agentSessionMocks.useAgentSessionWorkspace).toHaveBeenCalled();
    expect(agentSessionMocks.useAgentSessionWorkspace.mock.calls[0]?.[0]).toBe(baseSession.id);
    expect(agentSessionMocks.useAgentSessionWorkspace.mock.calls[0]?.[1]).toMatchObject({
      limit: 200,
      branch_mode: "head",
      refetchInterval: expect.any(Function),
    });
    expect(agentSessionMocks.useAgentSessionProjectionInfinite).toHaveBeenCalledWith(baseSession.id, {
      limit: 200,
      branch_mode: "head",
      enabled: true,
      initialPage: expect.objectContaining({
        focus_session_id: baseSession.id,
      }),
    });
    expect(agentSessionMocks.useAgentSessionTurns).toHaveBeenCalledWith(baseSession.id, {
      limit: 10,
      order: "desc",
      enabled: true,
      refetchInterval: 5_000,
    });
  });

  it("invalidates the workspace query itself when the SSE stream reports a change", () => {
    let handlers:
      | {
          onConnected?: (data?: { session_id: string; server_now_ms?: number }) => void;
          onWorkspaceChanged?: (data: {
            session_id: string;
            latest_event_id: number;
            thread_session_count: number;
            latest_event_emitted_at_ms?: number | null;
            server_fanout_at_ms?: number | null;
            server_now_ms?: number;
            pubsub_seq?: number;
          }) => void;
          onError?: () => void;
        }
      | undefined;
    streamMocks.connectSessionWorkspaceStream.mockImplementation((_sessionId, nextHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    });

    renderHook(() => useSessionWorkspace(baseSession.id));

    act(() => {
      handlers?.onConnected?.();
      handlers?.onWorkspaceChanged?.({
        session_id: baseSession.id,
        latest_event_id: 99,
        thread_session_count: 1,
      });
    });

    expect(queryClientMocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agent-session-workspace", baseSession.id],
    });
    expect(queryClientMocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agent-session-turns", baseSession.id],
    });
  });

  it("applies SSE transcript previews to the workspace cache before refetch", () => {
    let handlers:
      | {
          onConnected?: (data?: { session_id: string; server_now_ms?: number }) => void;
          onWorkspaceChanged?: (data: {
            session_id: string;
            latest_event_id: number;
            thread_session_count: number;
            latest_event_emitted_at_ms?: number | null;
            server_fanout_at_ms?: number | null;
            server_now_ms?: number;
            pubsub_seq?: number;
            transcript_preview?: {
              event_id: number;
              text: string;
              event_origin: string;
              timestamp: string;
              is_provisional: boolean;
              is_complete: boolean;
              content_cursor?: string | null;
              is_stale: boolean;
              stale_reason?: null;
            } | null;
          }) => void;
          onError?: () => void;
        }
      | undefined;
    streamMocks.connectSessionWorkspaceStream.mockImplementation((_sessionId, nextHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    });

    renderHook(() => useSessionWorkspace(baseSession.id));

    act(() => {
      handlers?.onWorkspaceChanged?.({
        session_id: baseSession.id,
        latest_event_id: 80,
        thread_session_count: 1,
        transcript_preview: {
          event_id: 321,
          text: "Immediate live preview",
          event_origin: "live_provisional",
          timestamp: "2026-03-14T12:01:21.000Z",
          is_provisional: true,
          is_complete: false,
          content_cursor: "cursor-321",
          is_stale: false,
          stale_reason: null,
        },
      });
    });

    expect(queryClientMocks.setQueriesData).toHaveBeenCalledWith(
      { queryKey: ["agent-session-workspace", baseSession.id] },
      expect.any(Function),
    );

    const updater = queryClientMocks.setQueriesData.mock.calls[0]?.[1];
    const current = agentSessionMocks.useAgentSessionWorkspace.mock.results[0]?.value.data;
    const updated = updater(current);
    expect(updated.session.transcript_preview).toMatchObject({
      text: "Immediate live preview",
      event_id: 321,
    });
    expect(updated.thread.sessions[0].transcript_preview).toMatchObject({
      text: "Immediate live preview",
    });
  });

  it("lets streamed transcript previews render before query refetch work starts", () => {
    const rafSpy = vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
    let handlers:
      | {
          onWorkspaceChanged?: (data: {
            session_id: string;
            latest_event_id: number;
            thread_session_count: number;
            transcript_preview?: {
              event_id: number;
              text: string;
              event_origin: string;
              timestamp: string;
              is_provisional: boolean;
              is_complete: boolean;
              content_cursor?: string | null;
              is_stale: boolean;
              stale_reason?: null;
            } | null;
          }) => void;
        }
      | undefined;
    streamMocks.connectSessionWorkspaceStream.mockImplementation((_sessionId, nextHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    });

    renderHook(() => useSessionWorkspace(baseSession.id));

    act(() => {
      handlers?.onWorkspaceChanged?.({
        session_id: baseSession.id,
        latest_event_id: 80,
        thread_session_count: 1,
        transcript_preview: {
          event_id: 321,
          text: "Paint before refetch",
          event_origin: "live_provisional",
          timestamp: "2026-03-14T12:01:21.000Z",
          is_provisional: true,
          is_complete: false,
          content_cursor: "cursor-321",
          is_stale: false,
          stale_reason: null,
        },
      });
    });

    expect(queryClientMocks.setQueriesData).toHaveBeenCalled();
    expect(queryClientMocks.invalidateQueries).not.toHaveBeenCalled();
    expect(rafSpy).toHaveBeenCalled();
    rafSpy.mockRestore();
  });

  it("does not defer refetch for backend-stale streamed transcript previews", () => {
    let handlers:
      | {
          onWorkspaceChanged?: (data: {
            session_id: string;
            latest_event_id: number;
            thread_session_count: number;
            transcript_preview?: {
              event_id: number;
              text: string;
              event_origin: string;
              timestamp: string;
              is_provisional: boolean;
              is_complete: boolean;
              content_cursor?: string | null;
              is_stale: boolean;
              stale_reason?: string | null;
            } | null;
          }) => void;
        }
      | undefined;
    streamMocks.connectSessionWorkspaceStream.mockImplementation((_sessionId, nextHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    });

    renderHook(() => useSessionWorkspace(baseSession.id));

    act(() => {
      handlers?.onWorkspaceChanged?.({
        session_id: baseSession.id,
        latest_event_id: 80,
        thread_session_count: 1,
        transcript_preview: {
          event_id: 321,
          text: "Expired preview must not delay durable refetch",
          event_origin: "live_provisional",
          timestamp: "2026-03-14T11:50:00.000Z",
          is_provisional: true,
          is_complete: false,
          content_cursor: "cursor-321",
          is_stale: true,
          stale_reason: "freshness_window_expired",
        },
      });
    });

    expect(queryClientMocks.setQueriesData).toHaveBeenCalled();
    expect(queryClientMocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agent-session-workspace", baseSession.id],
    });
  });

  it("keeps SSE transcript previews visible when query data is still stale", async () => {
    seedHookMocks(1);
    let handlers:
      | {
          onWorkspaceChanged?: (data: {
            session_id: string;
            latest_event_id: number;
            thread_session_count: number;
            transcript_preview?: {
              event_id: number;
              text: string;
              event_origin: string;
              timestamp: string;
              is_provisional: boolean;
              is_complete: boolean;
              content_cursor?: string | null;
              is_stale: boolean;
              stale_reason?: null;
            } | null;
          }) => void;
        }
      | undefined;
    streamMocks.connectSessionWorkspaceStream.mockImplementation((_sessionId, nextHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    });

    const { result } = renderHook(() => useSessionWorkspace(baseSession.id));

    act(() => {
      handlers?.onWorkspaceChanged?.({
        session_id: baseSession.id,
        latest_event_id: 80,
        thread_session_count: 1,
        transcript_preview: {
          event_id: 321,
          text: "Preview from SSE before refetch wins",
          event_origin: "live_provisional",
          timestamp: "2026-03-14T12:01:21.000Z",
          is_provisional: true,
          is_complete: false,
          content_cursor: "cursor-321",
          is_stale: false,
          stale_reason: null,
        },
      });
    });

    await waitFor(() => {
      expect(result.current.events.map((event) => event.content_text)).toContain(
        "Preview from SSE before refetch wins",
      );
    });
  });

  it("waits to emit render telemetry until the latest SSE event is in the rendered projection", async () => {
    let handlers:
      | {
          onConnected?: (data?: { session_id: string; server_now_ms?: number }) => void;
          onWorkspaceChanged?: (data: {
            session_id: string;
            latest_event_id: number;
            thread_session_count: number;
            latest_event_emitted_at_ms?: number | null;
            server_fanout_at_ms?: number | null;
            server_now_ms?: number;
            pubsub_seq?: number;
          }) => void;
          onError?: () => void;
        }
      | undefined;
    streamMocks.connectSessionWorkspaceStream.mockImplementation((_sessionId, nextHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    });

    seedHookMocks(80);
    const { rerender } = renderHook(() => useSessionWorkspace(baseSession.id));

    act(() => {
      handlers?.onWorkspaceChanged?.({
        session_id: baseSession.id,
        latest_event_id: 81,
        thread_session_count: 1,
        latest_event_emitted_at_ms: 1_779_220_000_000,
        server_fanout_at_ms: 1_779_220_000_150,
        server_now_ms: 1_779_220_000_100,
        pubsub_seq: 7,
      });
    });

    expect(renderBeaconMocks.emitRenderBeacon).not.toHaveBeenCalled();

    seedHookMocks(81);
    rerender();

    await waitFor(() => {
      expect(renderBeaconMocks.emitRenderBeacon).toHaveBeenCalledWith({
        sessionId: baseSession.id,
        latestEventId: 81,
        latestEventEmittedAtMs: 1_779_220_000_000,
        managed: false,
        serverFanoutAtMs: 1_779_220_000_150,
        clientReceivedAtMs: expect.any(Number),
        pubsubSeq: 7,
      });
    });
  });

  it("stops polling settled sessions when the document is hidden", () => {
    visibilityMocks.useDocumentVisible.mockReturnValue(false);
    agentSessionMocks.useAgentSessionWorkspace.mockReturnValue({
      data: {
        session: {
          ...baseSession,
          ended_at: "2026-03-14T12:10:00.000Z",
          terminal_state: "session_ended",
          status: "completed",
          runtime_display: { ...baseSession.runtime_display, lifecycle: "closed" },
        },
        thread: {
          sessions: [
            {
              ...baseSession,
              ended_at: "2026-03-14T12:10:00.000Z",
              terminal_state: "session_ended",
              status: "completed",
            },
          ],
          head_session_id: baseSession.id,
          root_session_id: baseSession.id,
        },
        projection: {
          root_session_id: baseSession.id,
          focus_session_id: baseSession.id,
          head_session_id: baseSession.id,
          path_session_ids: [baseSession.id],
          items: [],
          total: 0,
          abandoned_events: 0,
        },
      },
      isLoading: false,
      error: null,
    });

    renderHook(() => useSessionWorkspace(baseSession.id));

    const refetchInterval = agentSessionMocks.useAgentSessionWorkspace.mock.calls[0]?.[1]?.refetchInterval;
    expect(
      refetchInterval?.({
        state: {
          data: {
            session: {
              ...baseSession,
              ended_at: "2026-03-14T12:10:00.000Z",
              terminal_state: "session_ended",
              status: "completed",
            },
          },
        },
      }),
    ).toBe(false);
    expect(agentSessionMocks.useAgentSessionTurns).toHaveBeenCalledWith(baseSession.id, {
      limit: 10,
      order: "desc",
      enabled: false,
      refetchInterval: false,
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

    agentSessionMocks.useAgentSessionWorkspace.mockReturnValue({
      data: {
        session: childSession,
        thread: {
          sessions: [parentSession, childSession],
          head_session_id: childSession.id,
          root_session_id: parentSession.id,
        },
        projection: {
          root_session_id: parentSession.id,
          focus_session_id: childSession.id,
          head_session_id: childSession.id,
          path_session_ids: [parentSession.id, childSession.id],
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
      },
      isLoading: false,
      error: null,
    });
    agentSessionMocks.useAgentSessionProjectionInfinite.mockReturnValue({
      data: {
        pages: [
          {
            page_offset: 0,
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
      fetchPreviousPage: vi.fn(),
      hasPreviousPage: false,
      isFetchingPreviousPage: false,
    });

    const { result } = renderHook(() => useSessionWorkspace(childSession.id));

    expect(result.current.items[1]).toEqual({
      kind: "seam",
      seam: {
        key: "seam:session-child:2026-03-19T16:45:00Z",
        sessionId: "session-child",
        label: "Continuation begins",
        description: "Synced Local history above. New continuation messages below.",
        timestamp: "2026-03-19T16:45:00Z",
      },
    });
  });

  it("keeps older projection pages above the live tail window in display order", () => {
    const events = makeEvents(4);

    agentSessionMocks.useAgentSessionProjectionInfinite.mockReturnValue({
      data: {
        pages: [
          {
            page_offset: 2,
            items: events.slice(2).map((event) => ({
              kind: "event",
              session_id: baseSession.id,
              timestamp: event.timestamp,
              event,
            })),
            total: events.length,
            abandoned_events: 0,
          },
          {
            page_offset: 0,
            items: events.slice(0, 2).map((event) => ({
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
      fetchPreviousPage: vi.fn(),
      hasPreviousPage: false,
      isFetchingPreviousPage: false,
    });

    const { result } = renderHook(() => useSessionWorkspace(baseSession.id));

    expect(
      result.current.items
        .filter((item) => item.kind === "message")
        .map((item) => item.event.id),
    ).toEqual([1, 2, 3, 4]);
  });

  it("preserves evicted tail items when the live window shifts forward", () => {
    const events = makeEvents(5);
    const olderPage = {
      page_offset: 0,
      items: events.slice(0, 2).map((event) => ({
        kind: "event",
        session_id: baseSession.id,
        timestamp: event.timestamp,
        event,
      })),
      total: events.length,
      abandoned_events: 0,
    };
    const initialTailPage = {
      page_offset: 2,
      items: events.slice(2, 4).map((event) => ({
        kind: "event",
        session_id: baseSession.id,
        timestamp: event.timestamp,
        event,
      })),
      total: events.length,
      abandoned_events: 0,
    };
    const shiftedTailPage = {
      page_offset: 3,
      items: events.slice(3, 5).map((event) => ({
        kind: "event",
        session_id: baseSession.id,
        timestamp: event.timestamp,
        event,
      })),
      total: events.length,
      abandoned_events: 0,
    };

    let currentPages = [olderPage, initialTailPage];
    agentSessionMocks.useAgentSessionProjectionInfinite.mockImplementation(() => ({
      data: {
        pages: currentPages,
      },
      isLoading: false,
      error: null,
      fetchPreviousPage: vi.fn(),
      hasPreviousPage: false,
      isFetchingPreviousPage: false,
    }));

    const { result, rerender } = renderHook(() => useSessionWorkspace(baseSession.id));

    expect(
      result.current.items
        .filter((item) => item.kind === "message")
        .map((item) => item.event.id),
    ).toEqual([1, 2, 3, 4]);

    currentPages = [olderPage, shiftedTailPage];
    rerender();

    expect(
      result.current.items
        .filter((item) => item.kind === "message")
        .map((item) => item.event.id),
    ).toEqual([1, 2, 3, 4, 5]);
  });

  it("derives highlight selection from the projection model without mutating manual selection", () => {
    const { result, rerender } = renderHook(
      ({ highlightEventId }: { highlightEventId: number | null }) =>
        useSessionWorkspace(baseSession.id, { highlightEventId }),
      {
        initialProps: { highlightEventId: null },
      },
    );

    act(() => {
      result.current.selectKey("message:4");
    });

    expect(result.current.selectedKey).toBe("message:4");

    rerender({ highlightEventId: 2 });
    expect(result.current.selectedKey).toBe("message:2");

    rerender({ highlightEventId: null });
    expect(result.current.selectedKey).toBe("message:4");
  });

  // Search/filter state tests moved to TimelinePane component tests —
  // that state now lives inside TimelinePane, not the hook.
});
