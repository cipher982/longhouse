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

const { writeTextMock } = vi.hoisted(() => ({
  writeTextMock: vi.fn(),
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

function getRequestCallCount(pathSuffix: string) {
  return fetchWithRefreshMock.mock.calls.filter(([url]) => String(url).endsWith(pathSuffix)).length;
}

function getLastRequestBody(pathSuffix: string) {
  const call = [...fetchWithRefreshMock.mock.calls].reverse().find(([url]) => String(url).endsWith(pathSuffix));
  if (!call) {
    throw new Error(`Expected a ${pathSuffix} request`);
  }
  const options = call[1] as RequestInit | undefined;
  return JSON.parse(String(options?.body ?? "{}"));
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
    writeTextMock.mockReset();
    writeTextMock.mockResolvedValue(undefined);
    requestMock.mockImplementation((path: string) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: { writeText: writeTextMock },
    });
    Object.defineProperty(window.HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("renders a divider seam for the inline continuation dock", () => {
    const { container } = renderSessionChat({
      dockHeaderStyle: "divider",
      introEyebrow: "Cloud branch",
      introTitle: "Cloud branch began here",
      introDescription: "Earlier turns were synced from Local.",
      submitLabel: "Reply",
    });

    expect(screen.getByTestId("session-chat-divider")).toBeInTheDocument();
    expect(screen.getByText("Cloud branch began here")).toBeInTheDocument();
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

  it("keeps the dock visible but replaces disabled composer controls when control is offline", () => {
    renderSessionChat({
      composerDisabledReason: "Longhouse can see this session, but cannot send prompts until the engine reconnects.",
    });

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.getByTestId("session-chat-disabled-reason")).toHaveTextContent(
      "Control offline",
    );
    expect(screen.getByTestId("session-chat-disabled-reason")).toHaveTextContent(
      "cannot send prompts until the engine reconnects",
    );
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("replaces disabled full-panel composer controls with status copy", () => {
    renderSessionChat({
      layout: "panel",
      composerDisabledReason: "Longhouse can see this session, but cannot send prompts until the engine reconnects.",
    });

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.getByTestId("session-chat-disabled-reason")).toHaveTextContent(
      "Control offline",
    );
    expect(screen.getByTestId("session-chat-disabled-reason")).toHaveTextContent(
      "cannot send prompts until the engine reconnects",
    );
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("shows a managed-launch hint card for unmanaged sessions", () => {
    renderSessionChat({
      composerDisabledReason: "Live control is unavailable for this unmanaged Codex session.",
      managedLaunchSuggestion: {
        title: "Start the next Codex session through Longhouse",
        body: "This session stays searchable here. Use this command when you want the next Codex session to stay steerable from Longhouse.",
        command: "longhouse codex",
      },
    });

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.getByTestId("session-chat-managed-launch-hint")).toHaveTextContent(
      "Start the next Codex session through Longhouse",
    );
    expect(screen.getByTestId("session-chat-managed-launch-hint-command")).toHaveTextContent(
      "longhouse codex",
    );
    expect(screen.queryByTestId("session-chat-disabled-reason")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy command: longhouse codex/i })).toHaveTextContent(
      "Copy",
    );
  });

  it("shows empty-state copy instead of resume wording", () => {
    renderSessionChat({
      layout: "panel",
    });

    expect(screen.getByText("Start a conversation with this session.")).toBeInTheDocument();
    expect(
      screen.getByText("Earlier synced turns stay visible here. Your first message continues from that context."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/--resume/i)).not.toBeInTheDocument();
  });

  it("drafts a managed-local reply into an empty composer", async () => {
    const user = userEvent.setup();

    fetchWithRefreshMock.mockImplementation((url: string) => {
      if (url.endsWith("/draft-reply")) {
        return Promise.resolve(jsonResponse({ draft_text: "Ask for a concise status update." }));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderSessionChat({ chatMode: "managed_local" });

    await user.click(screen.getByRole("button", { name: /draft reply/i }));

    expect(getLastRequestBody("/draft-reply")).toEqual({ max_chars: 1200 });
    await waitFor(() => {
      expect(screen.getByRole("textbox")).toHaveValue("Ask for a concise status update.");
    });
    expect(screen.getByRole("button", { name: /send/i })).toBeEnabled();
  });

  it("keeps draft reply disabled once the composer has text", async () => {
    const user = userEvent.setup();

    renderSessionChat({ chatMode: "managed_local" });

    await user.type(screen.getByRole("textbox"), "I already know what to send");

    expect(screen.getByRole("button", { name: /draft reply/i })).toBeDisabled();
    expect(getRequestCallCount("/draft-reply")).toBe(0);
  });

  it("shows a clear message when a draft reply is empty", async () => {
    const user = userEvent.setup();

    fetchWithRefreshMock.mockImplementation((url: string) => {
      if (url.endsWith("/draft-reply")) {
        return Promise.resolve(jsonResponse({ draft_text: "   " }));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderSessionChat({ chatMode: "managed_local" });

    await user.click(screen.getByRole("button", { name: /draft reply/i }));

    expect(await screen.findByText("No draft suggestion available yet.")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("");
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
      if (url.endsWith("/send-live")) {
        return deferred.promise;
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderSessionChat({ chatMode: "managed_local" }, { queryClient });

    await user.type(screen.getByRole("textbox"), "Continue locally");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(getLastRequestBody("/send-live")).toEqual({
      message: "Continue locally",
    });
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

  it("requires an explicit click for the first message when configured", async () => {
    const user = userEvent.setup();
    const deferred = createDeferredResponse();

    fetchWithRefreshMock.mockImplementation((url: string) => {
      if (url.endsWith("/send-live")) {
        return deferred.promise;
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
      requireClickForFirstSend: true,
      keyboardHintText: "Click send to confirm.",
    });

    await user.type(screen.getByRole("textbox"), "Continue locally");
    await user.keyboard("{Enter}");

    expect(screen.getByTestId("session-chat-explicit-submit-hint")).toHaveTextContent(
      "Click send to confirm.",
    );
    expect(getRequestCallCount("/send-live")).toBe(0);

    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(getRequestCallCount("/send-live")).toBe(1));
  });
});
