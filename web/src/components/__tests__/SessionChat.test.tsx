import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, beforeEach, vi } from "vitest";
import { SessionChat, type SessionChatTarget } from "../SessionChat";
import type { SessionLockInfo } from "../../services/api";

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

vi.mock("../../services/api/base", () => {
  class ApiError extends Error {
    readonly status: number;
    readonly url: string;
    readonly body: unknown;
    constructor({ url, status, body }: { url: string; status: number; body: unknown }) {
      super(`Request failed (${status})`);
      this.name = "ApiError";
      this.status = status;
      this.url = url;
      this.body = body;
    }
  }
  return {
    buildUrl: (path: string) => path,
    request: requestMock,
    ApiError,
  };
});

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

  it("shows a manual interrupt affordance for stalled managed sessions", async () => {
    const user = userEvent.setup();
    let interruptCalls = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/interrupt-live") && init?.method === "POST") {
        interruptCalls += 1;
        return Promise.resolve({
          interrupt_dispatched: true,
          confirmed_stopped: false,
          session_id: "sess-1",
          exit_code: 0,
          error: null,
          released_lock: true,
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
      canQueueNextInput: true,
      isStalled: true,
    });

    const recovery = await screen.findByTestId("session-chat-stall-recovery");
    expect(recovery).toHaveTextContent(/managed session appears stalled/i);
    expect(screen.queryByText(/queue next auto-sends at the next turn boundary/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /interrupt/i }));
    await waitFor(() => expect(interruptCalls).toBe(1));
  });

  it("shows an inline Stop button for locked managed-local sessions", async () => {
    const user = userEvent.setup();
    let interruptCalls = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (String(path).endsWith("/interrupt-live") && init?.method === "POST") {
        interruptCalls += 1;
        return Promise.resolve({
          interrupt_dispatched: true,
          confirmed_stopped: false,
          session_id: "sess-1",
          exit_code: 0,
          error: null,
          released_lock: true,
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
    });

    expect(await screen.findByRole("button", { name: /stop/i })).toBeEnabled();
    expect(screen.getByText(/stop to interrupt/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /stop/i }));
    await waitFor(() => expect(interruptCalls).toBe(1));
  });

  it("does not mention Stop when a locked session is not interruptible", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(["session-lock", "sess-1"], {
      locked: true,
      holder: null,
      time_remaining_seconds: null,
      fork_available: true,
    });

    renderSessionChat({}, { queryClient });

    expect(screen.getByText(/you can draft the next message/i)).not.toHaveTextContent(/Stop/i);
    expect(screen.queryByRole("button", { name: /stop/i })).not.toBeInTheDocument();
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
      composerDisabledReason: "This unmanaged Codex session is read-only in Longhouse.",
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

  it("allows draft reply while the live session is working", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    queryClient.setQueryData<SessionLockInfo | null>(["session-lock", "sess-1"], {
      locked: true,
      holder: "req-1234",
      time_remaining_seconds: 120,
      fork_available: true,
    });

    fetchWithRefreshMock.mockImplementation((url: string) => {
      if (url.endsWith("/draft-reply")) {
        return Promise.resolve(jsonResponse({ draft_text: "Prepare a follow-up while it works." }));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderSessionChat({ chatMode: "managed_local" }, { queryClient });

    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText(/Agent is working/i)).toBeInTheDocument();
    expect(screen.queryByText(/req-1234/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/remaining/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /draft reply/i })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: /draft reply/i }));

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toHaveValue("Prepare a follow-up while it works.");
    });
    expect(screen.getByRole("textbox")).toBeEnabled();
    expect(screen.getByRole("button", { name: /waiting/i })).toBeDisabled();
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
    let resolveInput: ((value: unknown) => void) | null = null;
    const inputDeferred = new Promise((resolve) => {
      resolveInput = resolve;
    });
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    let lockReads = 0;

    requestMock.mockImplementation((path: string, init?: RequestInit) => {
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
      if (String(path).endsWith("/input") && init?.method === "POST") {
        return inputDeferred;
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({ chatMode: "managed_local" }, { queryClient });

    await user.type(screen.getByRole("textbox"), "Continue locally");
    await user.click(screen.getByRole("button", { name: /send/i }));

    const inputCall = requestMock.mock.calls.find(([path, init]) =>
      String(path).endsWith("/input") && (init as RequestInit | undefined)?.method === "POST",
    );
    expect(inputCall).toBeTruthy();
    const inputPayload = JSON.parse(String((inputCall?.[1] as RequestInit).body ?? "{}"));
    expect(inputPayload).toEqual({
      text: "Continue locally",
      intent: "auto",
      client_request_id: expect.stringMatching(/^web-/),
    });
    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeDisabled();
      expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
      expect(screen.getByText("Sending")).toBeInTheDocument();
      expect(screen.getByText("Continue locally")).toBeInTheDocument();
    });

    resolveInput?.({
      outcome: "sent",
      input_id: 1,
      intent: "auto",
      queued: [],
    });

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(8));
    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeEnabled();
      expect(screen.getByRole("button", { name: /waiting/i })).toBeDisabled();
      expect(screen.getByText("Working")).toBeInTheDocument();
    });
    expect(screen.queryByText(/req-1234/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/remaining/i)).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox"), "Next follow-up{enter}");
    expect(screen.getByRole("textbox")).toHaveValue("Next follow-up");
    const inputPostCount = requestMock.mock.calls.filter(
      ([path, init]) =>
        String(path).endsWith("/input") && (init as RequestInit | undefined)?.method === "POST",
    ).length;
    expect(inputPostCount).toBe(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["session-lock", "sess-1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-session-workspace", "sess-1"] });
  });

  it("requires an explicit click for the first message when configured", async () => {
    const user = userEvent.setup();
    let inputCalls = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        inputCalls += 1;
        return Promise.resolve({
          outcome: "sent",
          input_id: inputCalls,
          intent: "auto",
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
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
    expect(inputCalls).toBe(0);

    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(inputCalls).toBe(1));
  });

  it("queues an auto send when the session is locked and capability is on", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(["session-lock", "sess-1"], {
      locked: true,
      holder: null,
      time_remaining_seconds: null,
      fork_available: true,
    });

    let inputsReads = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (String(path).endsWith("/inputs") && !init) {
        inputsReads += 1;
        return Promise.resolve(
          inputsReads === 1
            ? []
            : [
                {
                  id: 42,
                  text: "wait for it",
                  intent: "auto",
                  status: "queued",
                  created_at: null,
                },
              ],
        );
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        return Promise.resolve({
          outcome: "queued",
          input_id: 42,
          intent: "auto",
          queued: [
            {
              id: 42,
              text: "wait for it",
              intent: "auto",
              status: "queued",
              created_at: null,
            },
          ],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat(
      { chatMode: "managed_local", canQueueNextInput: true },
      { queryClient },
    );

    // Lock notice adapts to the queue-next affordance.
    expect(
      screen.getByText(/queue next auto-sends at the next turn boundary/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /stop/i })).toBeEnabled();

    await user.type(screen.getByRole("textbox"), "wait for it");
    // Button says "Queue next" while working with queue capability, and is
    // enabled once a draft exists.
    const queueButton = await screen.findByRole("button", { name: /queue next/i });
    expect(queueButton).toBeEnabled();
    await user.click(queueButton);

    const chip = await screen.findByTestId("session-chat-queued");
    expect(chip).toHaveTextContent("wait for it");
    expect(chip).toHaveTextContent(/queued/i);
    expect(screen.getByRole("button", { name: /cancel queued message/i })).toBeEnabled();
  });

  it("shows Send update primary + Queue next secondary when steer capability is on", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(["session-lock", "sess-1"], {
      locked: true,
      holder: null,
      time_remaining_seconds: null,
      fork_available: true,
    });

    let steerCalls = 0;
    let queueCalls = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        if (payload.intent === "steer") {
          steerCalls += 1;
          return Promise.resolve({
            outcome: "sent",
            input_id: steerCalls,
            intent: "steer",
            queued: [],
          });
        }
        if (payload.intent === "queue") {
          queueCalls += 1;
          return Promise.resolve({
            outcome: "queued",
            input_id: 100 + queueCalls,
            intent: "queue",
            queued: [],
          });
        }
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat(
      {
        chatMode: "managed_local",
        canQueueNextInput: true,
        canSteerActiveTurn: true,
      },
      { queryClient },
    );

    await user.type(screen.getByRole("textbox"), "redirect the test");
    expect(screen.getByRole("button", { name: /send update/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /queue next/i })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: /send update/i }));
    await waitFor(() => expect(steerCalls).toBe(1));
    expect(queueCalls).toBe(0);
  });

  it("offers Queue instead after a steer fails with turn_ended", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(["session-lock", "sess-1"], {
      locked: true,
      holder: null,
      time_remaining_seconds: null,
      fork_available: true,
    });

    const { ApiError } = await import("../../services/api/base");

    let queueCalls = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        if (payload.intent === "steer") {
          return Promise.reject(
            new ApiError({
              url: String(path),
              status: 409,
              body: {
                detail: {
                  error_code: "turn_ended",
                  message: "The active turn already ended.",
                },
              },
            }),
          );
        }
        if (payload.intent === "queue") {
          queueCalls += 1;
          return Promise.resolve({
            outcome: "queued",
            input_id: 200 + queueCalls,
            intent: "queue",
            queued: [],
          });
        }
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat(
      {
        chatMode: "managed_local",
        canQueueNextInput: true,
        canSteerActiveTurn: true,
      },
      { queryClient },
    );

    await user.type(screen.getByRole("textbox"), "too late");
    await user.click(screen.getByRole("button", { name: /send update/i }));

    // Turn-ended prompt appears with the original text + a Queue instead action.
    const prompt = await screen.findByTestId("session-chat-turn-ended");
    expect(prompt).toHaveTextContent("too late");
    await user.click(screen.getByRole("button", { name: /queue instead/i }));
    await waitFor(() => expect(queueCalls).toBe(1));
  });

  it("does not silently queue when Enter is pressed while working", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(["session-lock", "sess-1"], {
      locked: true,
      holder: null,
      time_remaining_seconds: null,
      fork_available: true,
    });

    let postCalls = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        postCalls += 1;
        return Promise.resolve({
          outcome: "queued",
          input_id: postCalls,
          intent: "auto",
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat(
      { chatMode: "managed_local", canQueueNextInput: true },
      { queryClient },
    );

    await user.type(screen.getByRole("textbox"), "do not silently queue{enter}");
    expect(postCalls).toBe(0);
    expect(screen.getByRole("textbox")).toHaveValue("do not silently queue");
  });
});
