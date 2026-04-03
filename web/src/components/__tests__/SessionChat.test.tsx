import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, beforeEach, vi } from "vitest";
import { SessionChat, type SessionChatTarget } from "../SessionChat";

const { fetchWithRefreshMock } = vi.hoisted(() => ({
  fetchWithRefreshMock: vi.fn(),
}));

const { requestMock } = vi.hoisted(() => ({
  requestMock: vi.fn(),
}));

vi.mock("../../lib/auth-refresh", () => ({
  fetchWithRefresh: fetchWithRefreshMock,
}));

vi.mock("../../services/api/base", () => ({
  buildUrl: (path: string) => path,
  request: requestMock,
}));

function makeSession(overrides: Partial<SessionChatTarget> = {}): SessionChatTarget {
  return {
    id: "sess-1",
    project: "zerg",
    provider: "claude",
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

function mockSessionChatFetches(chatResponse: Response) {
  fetchWithRefreshMock.mockImplementation((url: string) => {
    if (url.endsWith("/chat")) {
      return Promise.resolve(chatResponse);
    }

    return Promise.reject(new Error(`Unexpected request: ${url}`));
  });
}

function getChatCallCount() {
  return fetchWithRefreshMock.mock.calls.filter(([url]) => String(url).endsWith("/chat")).length;
}

function createDeferredResponse() {
  let resolve: ((response: Response) => void) | null = null;
  const promise = new Promise<Response>((nextResolve) => {
    resolve = nextResolve;
  });
  return {
    promise,
    resolve(response: Response) {
      if (!resolve) {
        throw new Error("Deferred response already resolved");
      }
      resolve(response);
      resolve = null;
    },
  };
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
    requestMock.mockReset();
    requestMock.mockImplementation((path: string) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
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

  it("can hide the dock header when the boundary already lives in the timeline", () => {
    const { container } = renderSessionChat({
      dockHeaderStyle: "hidden",
    });

    expect(screen.queryByTestId("session-chat-divider")).not.toBeInTheDocument();
    expect(container.querySelector(".session-chat-callout")).toBeNull();
  });

  it("keeps the dock visible but disables the composer when browser control is unavailable", () => {
    renderSessionChat({
      composerDisabledReason: "This session is visible here, but Longhouse cannot continue it from the browser yet.",
    });

    expect(screen.getByRole("textbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByTestId("session-chat-disabled-reason")).toHaveTextContent(
      "Longhouse cannot continue it from the browser yet.",
    );
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("navigates only after persisted continuation events land", async () => {
    const user = userEvent.setup();
    const onSessionChanged = vi.fn();

    mockSessionChatFetches(
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
            sync_status: "complete",
            control_status: "completed",
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

  it("keeps a non-error placeholder while managed-local transcript sync is pending", async () => {
    const user = userEvent.setup();
    const onSessionChanged = vi.fn();

    mockSessionChatFetches(
      sseResponse([
        {
          event: "done",
          data: {
            session_id: "sess-2",
            shipped_session_id: "sess-2",
            created_continuation: true,
            persisted_events: 0,
            sync_status: "pending",
            control_status: "completed",
            exit_code: 0,
            total_text_length: 0,
            timestamp: "2026-03-24T12:00:00Z",
          },
        },
      ]),
    );

    renderSessionChat({ onSessionChanged });

    await user.type(screen.getByRole("textbox"), "Continue in cloud");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("Completed locally. Transcript syncing...")).toBeInTheDocument();
    expect(screen.queryByText(/could not save the continuation transcript/i)).not.toBeInTheDocument();
    expect(onSessionChanged).not.toHaveBeenCalled();
  });

  it("keeps the inline response visible when persistence fails", async () => {
    const user = userEvent.setup();
    const onSessionChanged = vi.fn();

    mockSessionChatFetches(
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
            sync_status: "failed",
            control_status: "completed",
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

  it("does not clear the dock scratchpad until same-session persistence completes", async () => {
    const user = userEvent.setup();
    const onSessionChanged = vi.fn();
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    mockSessionChatFetches(
      sseResponse([
        {
          event: "done",
          data: {
            session_id: "sess-1",
            shipped_session_id: "sess-1",
            created_continuation: false,
            persisted_events: 0,
            sync_status: "pending",
            control_status: "completed",
            exit_code: 0,
            total_text_length: 0,
            timestamp: "2026-03-24T12:05:00Z",
          },
        },
      ]),
    );

    renderSessionChat({ onSessionChanged }, { queryClient });

    await user.type(screen.getByRole("textbox"), "tesT: whats 2+2");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(
      await screen.findByText("Completed locally. Transcript syncing..."),
    ).toBeInTheDocument();
    expect(screen.getByText("tesT: whats 2+2")).toBeInTheDocument();
    expect(invalidateSpy).not.toHaveBeenCalled();
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

    mockSessionChatFetches(
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
            sync_status: "complete",
            control_status: "completed",
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

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(8));
    await waitFor(() => {
      expect(screen.queryByText("tesT: whats 2+2")).not.toBeInTheDocument();
      expect(screen.queryByText("4")).not.toBeInTheDocument();
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["session-lock", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session-workspace", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session-thread", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["agent-session-projection-infinite", "sess-1"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session-events", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["agent-session-events-infinite", "sess-1"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-sessions"] });
    expect(onSessionChanged).not.toHaveBeenCalled();
  });

  it("blocks duplicate input until a managed-local ack arrives, then refreshes all workspace caches", async () => {
    const user = userEvent.setup();
    const deferred = createDeferredResponse();
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    let lockReads = 0;

    requestMock.mockImplementation((path: string) => {
      if (String(path).endsWith("/lock")) {
        lockReads += 1;
        return Promise.resolve(
          lockReads === 1
            ? { locked: false, fork_available: false }
            : {
                locked: true,
                holder: "req-1234",
                time_remaining_seconds: 295,
                fork_available: true,
              },
        );
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    fetchWithRefreshMock.mockImplementation((url: string) => {
      if (url.endsWith("/chat")) {
        return deferred.promise;
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderSessionChat({ chatMode: "managed_local" }, { queryClient });

    await user.type(screen.getByRole("textbox"), "Continue locally");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeDisabled();
      expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
      expect(screen.getByText("Sending")).toBeInTheDocument();
      // Pending pill shows the message text while in-flight
      expect(screen.getByText("Continue locally")).toBeInTheDocument();
    });

    deferred.resolve(
      jsonResponse({
        accepted: true,
        session_id: "sess-1",
        request_id: "req-1234",
        dispatch_ms: 12.5,
      }),
    );

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(8));
    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeDisabled();
      expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
      expect(screen.getByText("Locked")).toBeInTheDocument();
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["session-lock", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session-workspace", "sess-1"] });
  });

  it("requires an explicit click for the first branching message", async () => {
    const user = userEvent.setup();

    mockSessionChatFetches(
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
            sync_status: "complete",
            control_status: "completed",
            exit_code: 0,
            total_text_length: 10,
            timestamp: "2026-03-19T16:46:17Z",
          },
        },
      ]),
    );

    renderSessionChat({
      requireClickForFirstSend: true,
      keyboardHintText: "Click send to start the branch.",
    });

    await user.type(screen.getByRole("textbox"), "Continue in cloud");
    await user.keyboard("{Enter}");

    expect(screen.getByTestId("session-chat-explicit-submit-hint")).toHaveTextContent(
      "Click send to start the branch.",
    );
    expect(getChatCallCount()).toBe(0);

    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(getChatCallCount()).toBe(1));
  });
});
