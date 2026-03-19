import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, beforeEach, vi } from "vitest";
import { SessionChat } from "../SessionChat";
import type { ActiveSession } from "../../hooks/useActiveSessions";

const { fetchWithRefreshMock } = vi.hoisted(() => ({
  fetchWithRefreshMock: vi.fn(),
}));

vi.mock("../../lib/auth-refresh", () => ({
  fetchWithRefresh: fetchWithRefreshMock,
}));

vi.mock("../../services/api/base", () => ({
  buildUrl: (path: string) => path,
}));

function makeSession(overrides: Partial<ActiveSession> = {}): ActiveSession {
  return {
    id: "sess-1",
    project: "zerg",
    provider: "claude",
    cwd: "/Users/davidrose/git/zerg",
    git_repo: "git@github.com:cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-03-19T16:45:00Z",
    ended_at: null,
    last_activity_at: "2026-03-19T16:45:00Z",
    status: "working",
    attention: "auto",
    duration_minutes: 0,
    last_user_message: null,
    last_assistant_message: null,
    message_count: 0,
    tool_calls: 0,
    presence_state: null,
    presence_tool: null,
    presence_updated_at: null,
    user_state: "active",
    loop_mode: "manual",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status: number = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(events: Array<{ event: string; data: unknown }>): Response {
  const encoder = new TextEncoder();
  const payload = events
    .map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join("");

  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(payload));
        controller.close();
      },
    }),
    {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    },
  );
}

function renderSessionChat(
  props: Partial<React.ComponentProps<typeof SessionChat>> = {},
  options: { queryClient?: QueryClient } = {},
) {
  const queryClient =
    options.queryClient ??
    new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });

  const defaultProps: React.ComponentProps<typeof SessionChat> = {
    session: makeSession(),
    layout: "dock",
    ...props,
  };

  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <SessionChat {...defaultProps} />
      </QueryClientProvider>,
    ),
  };
}

describe("SessionChat", () => {
  beforeEach(() => {
    fetchWithRefreshMock.mockReset();
    Object.defineProperty(window.HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("renders a divider seam for the inline continuation dock", () => {
    const { container } = renderSessionChat({
      dockHeaderStyle: "divider",
      introEyebrow: "Cloud continuation",
      introTitle: "Cloud continuation began here",
      introDescription: "Earlier turns were synced from Local.",
      submitLabel: "Reply",
    });

    expect(screen.getByTestId("session-chat-divider")).toBeInTheDocument();
    expect(screen.getByText("Cloud continuation began here")).toBeInTheDocument();
    expect(screen.getByText("Earlier turns were synced from Local.")).toBeInTheDocument();
    expect(container.querySelector(".session-chat-callout")).toBeNull();
    expect(screen.getByRole("button", { name: "Reply" })).toBeInTheDocument();
  });

  it("navigates only after persisted continuation events land", async () => {
    const user = userEvent.setup();
    const onSessionChanged = vi.fn();

    fetchWithRefreshMock
      .mockResolvedValueOnce(jsonResponse({ locked: false, fork_available: false }))
      .mockResolvedValueOnce(
        sseResponse([
          {
            event: "assistant_delta",
            data: { text: "Saved reply", accumulated: "Saved reply" },
          },
          {
            event: "done",
            data: {
              session_id: "sess-2",
              shipped_session_id: "sess-2",
              created_continuation: true,
              persisted_events: 4,
              exit_code: 0,
              total_text_length: 10,
              timestamp: "2026-03-19T16:46:17Z",
            },
          },
        ]),
      );

    renderSessionChat({ onSessionChanged });

    await user.type(screen.getByRole("textbox"), "Continue in cloud");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(onSessionChanged).toHaveBeenCalledWith("sess-2", true));
    expect(screen.queryByText(/could not save the continuation transcript/i)).not.toBeInTheDocument();
  });

  it("keeps the inline response visible when persistence fails", async () => {
    const user = userEvent.setup();
    const onSessionChanged = vi.fn();

    fetchWithRefreshMock
      .mockResolvedValueOnce(jsonResponse({ locked: false, fork_available: false }))
      .mockResolvedValueOnce(
        sseResponse([
          {
            event: "assistant_delta",
            data: { text: "Saved nowhere", accumulated: "Saved nowhere" },
          },
          {
            event: "done",
            data: {
              session_id: "sess-2",
              shipped_session_id: null,
              created_continuation: true,
              persisted_events: 0,
              persistence_error:
                "Response completed, but Longhouse could not save the continuation transcript to the timeline.",
              exit_code: 0,
              total_text_length: 12,
              timestamp: "2026-03-19T16:46:17Z",
            },
          },
        ]),
      );

    renderSessionChat({ onSessionChanged });

    await user.type(screen.getByRole("textbox"), "Continue in cloud");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("Saved nowhere")).toBeInTheDocument();
    expect(await screen.findByText(/could not save the continuation transcript/i)).toBeInTheDocument();
    expect(onSessionChanged).not.toHaveBeenCalled();
  });

  it("refreshes the transcript and clears the dock scratchpad after same-session persistence", async () => {
    const user = userEvent.setup();
    const onSessionChanged = vi.fn();
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    fetchWithRefreshMock
      .mockResolvedValueOnce(jsonResponse({ locked: false, fork_available: false }))
      .mockResolvedValueOnce(
        sseResponse([
          {
            event: "assistant_delta",
            data: { text: "4", accumulated: "4" },
          },
          {
            event: "done",
            data: {
              session_id: "sess-1",
              shipped_session_id: "sess-1",
              created_continuation: false,
              persisted_events: 4,
              exit_code: 0,
              total_text_length: 1,
              timestamp: "2026-03-19T16:46:17Z",
            },
          },
        ]),
      );

    renderSessionChat({ onSessionChanged }, { queryClient });

    await user.type(screen.getByRole("textbox"), "tesT: whats 2+2");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(5));
    await waitFor(() => {
      expect(screen.queryByText("tesT: whats 2+2")).not.toBeInTheDocument();
      expect(screen.queryByText("4")).not.toBeInTheDocument();
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session-thread", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session-events", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["agent-session-events-infinite", "sess-1"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-sessions"] });
    expect(onSessionChanged).not.toHaveBeenCalled();
  });
});
