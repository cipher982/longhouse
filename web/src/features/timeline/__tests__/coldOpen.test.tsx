import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TimelineSessionCard, TimelineSessionsListResponse } from "@/shared/api/agents";
import { makeSessionStateFacts } from "@/shared/test/sessionState";

const preload = vi.fn();
vi.mock("@/app/routeChunks", () => ({
  preloadSessionDetailPage: () => preload(),
  useIdlePreloadSessionDetailPage: () => {},
}));
const railPrefetch = vi.fn();
vi.mock("@/features/session/rail/useRailPrefetch", () => ({
  useRailPrefetch: (ids: readonly string[], active: string | null) => railPrefetch(ids, active),
}));

import { SessionRow } from "../SessionRow";
import { TimelineInbox } from "../TimelineInbox";
import { SessionOpening, findListedSession } from "@/features/session/SessionOpening";

const NOW = Date.parse("2026-05-19T16:00:00Z");

function card(id: string, minutesAgo: number, title = `Session ${id}`): TimelineSessionCard {
  const at = new Date(NOW - minutesAgo * 60_000).toISOString();
  return {
    thread_id: `thread-${id}`,
    head: {
      id,
      provider: "claude",
      project: "zerg",
      device_id: "cinder",
      started_at: at,
      last_activity_at: at,
      timeline_title: title,
      user_messages: 3,
      assistant_messages: 4,
      tool_calls: 9,
      session_state: makeSessionStateFacts({ activity: "idle", observedAt: at }),
    },
  } as unknown as TimelineSessionCard;
}

// jsdom's PointerEvent drops `button`; a pointerdown MouseEvent carries it.
function press(el: Element, button: number) {
  fireEvent(el, new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button }));
}

beforeEach(() => {
  preload.mockReset();
  railPrefetch.mockReset();
});

describe("cold open: pressing a row", () => {
  it("starts the transcript and the page code on pointer down, after dnd-kit's own handler", () => {
    const onPrefetch = vi.fn();
    const dndPointerDown = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionRow
          thread={card("a", 1)}
          onClick={() => {}}
          onPrefetch={onPrefetch}
          sortableListeners={{ onPointerDown: dndPointerDown }}
          relativeNowMs={NOW}
        />
      </QueryClientProvider>,
    );
    press(screen.getByTestId("session-row"), 0);
    expect(dndPointerDown).toHaveBeenCalledTimes(1);
    expect(onPrefetch).toHaveBeenCalledTimes(1);
    expect(preload).toHaveBeenCalledTimes(1);
  });

  it("ignores a secondary-button press", () => {
    const onPrefetch = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionRow thread={card("a", 1)} onClick={() => {}} onPrefetch={onPrefetch} relativeNowMs={NOW} />
      </QueryClientProvider>,
    );
    press(screen.getByTestId("session-row"), 2);
    expect(onPrefetch).not.toHaveBeenCalled();
  });
});

describe("cold open: an idle Timeline warms its first rows", () => {
  it("warms the first five sessions in the order the Timeline shows them", () => {
    const sessions = Array.from({ length: 8 }, (_, i) => card(`s${i}`, 60 * 24 * (i + 2)));
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <TimelineInbox sessions={sessions} relativeNowMs={NOW} onSessionClick={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const [ids, active] = railPrefetch.mock.calls.at(-1) ?? [];
    expect(active).toBeNull();
    expect(ids).toHaveLength(5);
    const shown = screen.getAllByTestId("session-row").map((row) => row.getAttribute("data-session-id"));
    expect(ids).toEqual(shown.slice(0, 5));
  });
});

describe("cold open: the frame before the transcript", () => {
  function clientWithList(list: TimelineSessionCard[]) {
    const queryClient = new QueryClient();
    const data: TimelineSessionsListResponse = { sessions: list, total: list.length, has_real_sessions: true };
    queryClient.setQueryData(["agent-sessions", { limit: 50 }], data);
    return queryClient;
  }

  it("finds the session in any cached Timeline list", () => {
    const queryClient = clientWithList([card("a", 1), card("b", 2)]);
    expect(findListedSession(queryClient, "b")?.id).toBe("b");
    expect(findListedSession(queryClient, "missing")).toBeNull();
  });

  it("shows the listed title and meta in the app bar, with placeholder rows", () => {
    const queryClient = clientWithList([card("a", 1, "Fix the flaky reconnect test")]);
    const slot = document.createElement("div");
    document.body.appendChild(slot);
    render(
      <QueryClientProvider client={queryClient}>
        <SessionOpening sessionId="a" headerTarget={slot} onBack={() => {}} />
      </QueryClientProvider>,
    );
    expect(slot).toHaveTextContent("Fix the flaky reconnect test");
    expect(slot).toHaveTextContent("7 msgs");
    expect(screen.getByTestId("session-opening")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText(/Loading session/)).toBeNull();
    slot.remove();
  });

  it("still shows a frame when the session was never listed", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionOpening sessionId="unknown" headerTarget={null} onBack={() => {}} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("session-opening-header")).toHaveTextContent("Opening session…");
  });
});
