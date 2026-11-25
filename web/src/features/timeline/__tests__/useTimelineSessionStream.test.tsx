import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTimelineSessionStream } from "../useTimelineSessionStream";
import type { AgentSessionFilters, TimelineSessionCard, TimelineSessionsListResponse } from "@/shared/api/agents";

class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  options: EventSourceInit | undefined;
  close = vi.fn();

  constructor(url: string, options?: EventSourceInit) {
    this.url = url;
    this.options = options;
    MockEventSource.instances.push(this);
  }

  addEventListener = vi.fn();
}

function Harness({
  filters,
  options,
}: {
  filters: AgentSessionFilters;
  options: { enabled?: boolean; skipInitialReplay?: boolean };
}) {
  useTimelineSessionStream(filters, options);
  return null;
}

function renderStream(filters: AgentSessionFilters, options: { enabled?: boolean; skipInitialReplay?: boolean }) {
  const queryClient = new QueryClient();
  return {
    queryClient,
    view: render(
      <QueryClientProvider client={queryClient}>
        <Harness filters={filters} options={options} />
      </QueryClientProvider>,
    ),
  };
}

describe("useTimelineSessionStream", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not reconnect when only the one-shot initial replay flag changes", () => {
    const filters = { project: "zerg", provider: "codex", days_back: 14, limit: 50 };
    const { queryClient, view } = renderStream(filters, { enabled: true, skipInitialReplay: true });

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toContain("skip_initial_replay=true");

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <Harness filters={filters} options={{ enabled: true, skipInitialReplay: false }} />
      </QueryClientProvider>,
    );

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].close).not.toHaveBeenCalled();
  });

  it("keeps older unread sessions pinned above the history limit on SSE upserts", () => {
    const filters = { limit: 2 };
    const { queryClient } = renderStream(filters, { enabled: true });

    const makeCard = (id: string, anchor: string, unread: boolean): TimelineSessionCard =>
      ({
        thread_id: id,
        head: {
          id,
          started_at: anchor,
          last_activity_at: anchor,
          ended_at: anchor,
          session_state: {
            working_set: "history",
            unread,
            disposition: { state: "open", reason: null },
            activity: { state: "quiescent" },
            presentation: { primary: null, signal: "idle" },
          },
        },
      }) as unknown as TimelineSessionCard;

    queryClient.setQueryData<TimelineSessionsListResponse>(["agent-sessions", filters], {
      sessions: [
        makeCard("older-unread", "2026-11-20T10:00:00Z", true),
        makeCard("recent-1", "2026-11-25T10:00:00Z", false),
      ],
      total: 2,
      has_real_sessions: true,
    });

    const source = MockEventSource.instances[0];
    const upsertCall = source.addEventListener.mock.calls.find(([name]) => name === "session_upsert");
    expect(upsertCall).toBeDefined();
    const handler = upsertCall![1] as (event: MessageEvent) => void;
    handler(
      new MessageEvent("session_upsert", {
        data: JSON.stringify({
          session: makeCard("recent-2", "2026-11-25T11:00:00Z", false),
          total: 3,
          has_real_sessions: true,
        }),
      }),
    );

    const updated = queryClient.getQueryData<TimelineSessionsListResponse>(["agent-sessions", filters]);
    expect(updated?.sessions.map((s) => s.thread_id)).toEqual(["older-unread", "recent-2"]);
  });
});
