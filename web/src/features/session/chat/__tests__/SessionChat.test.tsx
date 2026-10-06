import { useCallback, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/shared/api/base";
import { describe, expect, it, beforeEach, vi } from "vitest";
import { SessionChat, type SessionChatTarget } from "../SessionChat";
import type { SessionLockInfo } from "@/shared/api/index";
import type { TimelineItem } from "@/shared/session/model";
import { makeSessionStateFacts } from "@/shared/test/sessionState";
import type { OutboxEntry } from "../../OutboxRow";
import { HOST_LINK_COPY } from "@/shared/hostLink/copy";
import { hostLinkStore } from "@/shared/hostLink/store";

const { fetchWithRefreshMock } = vi.hoisted(() => ({
  fetchWithRefreshMock: vi.fn(),
}));

const { requestMock } = vi.hoisted(() => ({
  requestMock: vi.fn(),
}));

const { writeTextMock } = vi.hoisted(() => ({
  writeTextMock: vi.fn(),
}));

vi.mock("@/features/auth/auth-refresh", () => ({
  fetchWithRefresh: fetchWithRefreshMock,
}));

vi.mock("@/shared/api/base", () => {
  class ApiError extends Error {
    readonly status: number;
    readonly url: string;
    readonly body: unknown;
    constructor({
      url,
      status,
      body,
    }: {
      url: string;
      status: number;
      body: unknown;
    }) {
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

vi.mock("../imageCompression", () => ({
  ImageCompressionError: class ImageCompressionError extends Error {},
  compressImageForUpload: vi.fn(async (file: File) => ({
    blob: file,
    byteSize: file.size,
    mimeType: file.type,
  })),
}));

function makeSession(
  overrides: Partial<SessionChatTarget> = {},
): SessionChatTarget {
  return {
    id: "sess-1",
    project: "zerg",
    provider: "claude",
    device_id: null,
    session_state: makeSessionStateFacts({
      access: "live_control",
      interruptAvailable: true,
    }),
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
  return fetchWithRefreshMock.mock.calls.filter(([url]) =>
    String(url).endsWith(pathSuffix),
  ).length;
}

function getLastRequestBody(pathSuffix: string) {
  const call = [...fetchWithRefreshMock.mock.calls]
    .reverse()
    .find(([url]) => String(url).endsWith(pathSuffix));
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

function waitForDuration(milliseconds: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, milliseconds);
  return promise;
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

  const renderResult = render(
    <QueryClientProvider client={queryClient}>
      <SessionChat {...defaultProps} />
    </QueryClientProvider>,
  );

  return {
    queryClient,
    ...renderResult,
    rerenderSessionChat(
      nextProps: Partial<React.ComponentProps<typeof SessionChat>>,
    ) {
      renderResult.rerender(
        <QueryClientProvider client={queryClient}>
          <SessionChat {...defaultProps} {...nextProps} />
        </QueryClientProvider>,
      );
    },
  };
}

function makeLonghouseUserItem({
  sessionInputId,
  clientRequestId,
  text = "server projected text",
  authoredVia = "longhouse",
}: {
  sessionInputId?: number | null;
  clientRequestId?: string | null;
  text?: string;
  authoredVia?: "longhouse" | "terminal" | null;
}): TimelineItem {
  return {
    kind: "message",
    event: {
      id: 99,
      role: "user",
      content_text: text,
      tool_name: null,
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: null,
      timestamp: "2026-05-15T20:00:00Z",
      in_active_context: true,
      is_head_branch: true,
      input_origin: authoredVia
        ? {
            authored_via: authoredVia,
            session_input_id: sessionInputId,
            client_request_id: clientRequestId,
          }
        : null,
    },
  };
}

describe("SessionChat", () => {
  beforeEach(() => {
    hostLinkStore.observeLifecycle({
      state: "serving",
      runtime_epoch: "session-chat-test",
    });
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
    URL.createObjectURL = vi.fn(() => "blob:test-preview");
    URL.revokeObjectURL = vi.fn();
    window.localStorage.clear();
  });

  it("shows the Console model chip and sends its selected model", async () => {
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/providers/codex/models")) {
        return Promise.resolve({
          device_id: "cinder",
          provider: "codex",
          models: [
            { model: "gpt-5.6-luna", last_used_at: "2026-09-25T12:00:00Z" },
          ],
        });
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const body = JSON.parse(String(init.body ?? "{}")) as {
          client_request_id?: string;
        };
        return Promise.resolve({
          outcome: "sent",
          input_id: 1,
          intent: "auto",
          client_request_id: body.client_request_id,
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    const user = userEvent.setup();
    renderSessionChat({
      chatMode: "managed_local",
      session: makeSession({
        device_id: "cinder",
        provider: "codex",
        selected_model: "gpt-5.5",
        session_state: makeSessionStateFacts({
          access: "live_control",
          mode: "console",
        }),
      }),
    });

    expect(await screen.findByTestId("session-model-select")).toHaveTextContent(
      "gpt-5.5",
    );
    await user.click(
      screen.getByTestId("session-model-select").querySelector("summary")!,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /gpt-5\.6-luna/ })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /gpt-5\.6-luna/ }));
    await user.type(
      screen.getByRole("textbox", { name: "Next instruction" }),
      "Continue with this model",
    );
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      const inputCall = requestMock.mock.calls.find(
        ([path, init]) =>
          String(path).endsWith("/input") &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(inputCall).toBeTruthy();
      expect(JSON.parse(String((inputCall?.[1] as RequestInit).body))).toMatchObject({
        model: "gpt-5.6-luna",
      });
    });
  });

  it("hydrates a late session model without replacing a user's multipart choice", async () => {
    const user = userEvent.setup();
    let multipartBody: FormData | null = null;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      const requestPath = String(path);
      if (requestPath.endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (requestPath.endsWith("/providers/codex/models")) {
        return Promise.resolve({
          device_id: "cinder",
          provider: "codex",
          models: [
            { model: "gpt-5.6-luna", last_used_at: "2026-09-25T12:00:00Z" },
            { model: "gpt-5.5", last_used_at: "2026-09-24T12:00:00Z" },
            { model: "gpt-5.4-mini", last_used_at: "2026-09-23T12:00:00Z" },
          ],
        });
      }
      if (requestPath.endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (
        requestPath.endsWith("/inputs-multipart") &&
        init?.method === "POST"
      ) {
        multipartBody = init.body as FormData;
        return Promise.resolve({
          disposition: "accepted",
          outcome: "sent",
          intent: "auto",
          client_request_id: multipartBody.get("client_request_id"),
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    const consoleSession = (selected_model?: string) =>
      makeSession({
        device_id: "cinder",
        provider: "codex",
        selected_model,
        capabilities: { attach_images: true },
        session_state: makeSessionStateFacts({
          access: "live_control",
          mode: "console",
        }),
      });
    const view = renderSessionChat({
      chatMode: "managed_local",
      timelineItems: [],
      session: consoleSession(),
    });
    await screen.findByTestId("session-model-select");

    view.rerenderSessionChat({ session: consoleSession("gpt-5.4-mini") });
    await waitFor(() =>
      expect(screen.getByTestId("session-model-select")).toHaveTextContent(
        "gpt-5.4-mini",
      ),
    );
    await user.click(
      screen.getByTestId("session-model-select").querySelector("summary")!,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /gpt-5\.6-luna/ })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /gpt-5\.6-luna/ }));

    view.rerenderSessionChat({ session: consoleSession("gpt-5.5") });
    await waitFor(() =>
      expect(screen.getByTestId("session-model-select")).toHaveTextContent(
        "gpt-5.6-luna",
      ),
    );

    const input = view.container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement | null;
    expect(input).toBeTruthy();
    await user.upload(
      input!,
      new File([new Uint8Array([1, 2, 3])], "reference.png", {
        type: "image/png",
      }),
    );
    await user.type(
      screen.getByRole("textbox", { name: "Next instruction" }),
      "use the chosen model",
    );
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(multipartBody).not.toBeNull());
    expect(multipartBody?.get("model")).toBe("gpt-5.6-luna");
    expect(multipartBody?.get("text")).toBe("use the chosen model");
  });
  it("expires turn-specific actions without another update while retaining the draft", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-10T12:00:00Z"));
    const view = renderSessionChat({
      chatMode: "managed_local",
      canSteerActiveTurn: true,
      canQueueNextInput: true,
      session: makeSession({
        session_state: makeSessionStateFacts({
          activity: "executing",
          access: "live_control",
          interruptAvailable: true,
          activityValidUntil: "2026-09-10T12:00:01.500Z",
        }),
      }),
    });
    try {
      const draft = screen.getByRole("textbox");
      fireEvent.change(draft, { target: { value: "Keep this instruction" } });
      draft.focus();
      expect(
        screen.getByRole("button", { name: /send update/i }),
      ).toBeEnabled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_000);
      });
      expect(
        screen.queryByRole("button", { name: /send update/i }),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
      expect(screen.getByRole("textbox")).toBe(draft);
      expect(draft).toHaveValue("Keep this instruction");
      expect(draft).toHaveFocus();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
  it("retains an editable draft when control is revoked without allowing a send", async () => {
    const user = userEvent.setup();
    const view = renderSessionChat({
      chatMode: "managed_local",
      session: makeSession({
        session_state: makeSessionStateFacts({
          activity: "executing",
          access: "live_control",
          interruptAvailable: true,
        }),
      }),
      canSteerActiveTurn: true,
      canQueueNextInput: true,
    });
    const draft = screen.getByRole("textbox");
    await user.type(draft, "Preserve this instruction");
    view.rerenderSessionChat({
      composerDisabledReason: "Answer in the terminal first.",
    });
    expect(screen.getByRole("textbox")).toBe(draft);
    expect(draft).toHaveFocus();
    expect(draft).toHaveValue("Preserve this instruction");
    expect(screen.getByRole("button", { name: "Send update" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Queue next" })).toBeDisabled();
    view.rerenderSessionChat({ composerDisabledReason: null });
    expect(screen.getByRole("textbox")).toBe(draft);
    expect(draft).toHaveValue("Preserve this instruction");
  });

  it("shows recovery after a live composer becomes unavailable and restores its draft", async () => {
    const user = userEvent.setup();
    const view = renderSessionChat({ chatMode: "managed_local" });
    await user.type(screen.getByRole("textbox"), "Keep this instruction");

    view.rerenderSessionChat({
      composerDisabledReason: "The run ended.",
      composerDisabledAction: <button type="button">Show resume command</button>,
    });

    expect(screen.getByRole("button", { name: "Show resume command" })).toBeEnabled();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    view.rerenderSessionChat({
      composerDisabledReason: null,
      composerDisabledAction: null,
    });

    expect(screen.getByRole("textbox")).toHaveValue("Keep this instruction");
    expect(screen.queryByRole("button", { name: "Show resume command" })).not.toBeInTheDocument();
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
      session: makeSession({
        session_state: makeSessionStateFacts({
          activity: "stalled",
          access: "live_control",
          interruptAvailable: true,
        }),
      }),
    });

    const recovery = await screen.findByTestId("session-chat-stall-recovery");
    expect(recovery).toHaveTextContent(/managed session appears stalled/i);
    expect(
      screen.queryByText(/queue next auto-sends at the next turn boundary/i),
    ).not.toBeInTheDocument();

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
    await user.click(screen.getByRole("button", { name: /stop/i }));
    await waitFor(() => expect(interruptCalls).toBe(1));
  });

  it("routes Console Stop to the turn-scoped interrupt endpoint", async () => {
    const user = userEvent.setup();
    let consoleInterruptCalls = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (
        String(path).endsWith("/turns/current/interrupt") &&
        init?.method === "POST"
      ) {
        consoleInterruptCalls += 1;
        return Promise.resolve({
          interrupt_dispatched: true,
          session_id: "sess-1",
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
      session: makeSession({
        provider: "cursor",
        session_state: makeSessionStateFacts({
          activity: "executing",
          access: "live_control",
          mode: "console",
          startTurnAvailable: true,
          sendAvailable: false,
          interruptAvailable: true,
        }),
        capabilities: {
          control_label: "console",
          can_interrupt_active_turn: true,
        } as SessionChatTarget["capabilities"],
      }),
    });

    await user.click(await screen.findByRole("button", { name: /stop/i }));
    await waitFor(() => expect(consoleInterruptCalls).toBe(1));
  });

  it("does not mention Stop when a locked session is not interruptible", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(
      ["session-lock", "sess-1"],
      {
        locked: true,
        holder: null,
        time_remaining_seconds: null,
        fork_available: true,
      },
    );

    renderSessionChat({}, { queryClient });

    expect(
      screen.queryByRole("button", { name: /stop/i }),
    ).not.toBeInTheDocument();
  });

  it("keeps the dock visible but shows the blocker it was given, not a hardcoded heading", () => {
    renderSessionChat({
      composerDisabledTitle: "Can't send",
      composerDisabledReason:
        "This session's machine isn't accepting new Codex turns.",
    });

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Send" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("session-chat-disabled-reason"),
    ).toHaveTextContent("Can't send");
    expect(
      screen.getByTestId("session-chat-disabled-reason"),
    ).toHaveTextContent("isn't accepting new Codex turns");
    // The heading used to be the literal string "Control offline" regardless of
    // the blocker, which is how a connected machine got told to reconnect.
    expect(
      screen.getByTestId("session-chat-disabled-reason"),
    ).not.toHaveTextContent("Control offline");
  });

  it("replaces disabled full-panel composer controls with status copy", () => {
    renderSessionChat({
      layout: "panel",
      composerDisabledTitle: "Machine offline",
      composerDisabledReason:
        "The machine running this Codex session is offline. Sending resumes when it reconnects.",
    });

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Send" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("session-chat-disabled-reason"),
    ).toHaveTextContent("Machine offline");
    expect(
      screen.getByTestId("session-chat-disabled-reason"),
    ).toHaveTextContent("Sending resumes when it reconnects");
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("shows a managed-launch hint card for unmanaged sessions", () => {
    renderSessionChat({
      composerDisabledReason:
        "This unmanaged Codex session is read-only in Longhouse.",
      managedLaunchSuggestion: {
        title: "Start the next Codex session through Longhouse",
        body: "This session stays searchable here. Use this command when you want the next Codex session to stay steerable from Longhouse.",
        command: "longhouse codex",
      },
    });

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Send" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("session-chat-managed-launch-hint"),
    ).toHaveTextContent("Start the next Codex session through Longhouse");
    expect(
      screen.getByTestId("session-chat-managed-launch-hint-command"),
    ).toHaveTextContent("longhouse codex");
    expect(
      screen.queryByTestId("session-chat-disabled-reason"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /copy command: longhouse codex/i }),
    ).toHaveTextContent("Copy");
  });

  it("shows empty-state copy instead of resume wording", () => {
    renderSessionChat({
      layout: "panel",
    });

    expect(
      screen.getByText("Start a conversation with this session."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Earlier synced turns stay visible here. Your first message continues from that context.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/--resume/i)).not.toBeInTheDocument();
  });

  it("blocks duplicate input until a managed-local ack arrives", async () => {
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

    const inputCall = requestMock.mock.calls.find(
      ([path, init]) =>
        String(path).endsWith("/input") &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(inputCall).toBeTruthy();
    const inputPayload = JSON.parse(
      String((inputCall?.[1] as RequestInit).body ?? "{}"),
    );
    expect(inputPayload).toEqual({
      text: "Continue locally",
      intent: "auto",
      client_request_id: expect.stringMatching(/^web-/),
    });
    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeDisabled();
      expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
      expect(screen.getByText("Delivering...")).toBeInTheDocument();
      expect(screen.getByText("Continue locally")).toBeInTheDocument();
    });

    resolveInput?.({
      outcome: "sent",
      input_id: 1,
      intent: "auto",
      client_request_id: inputPayload.client_request_id,
      queued: [],
    });

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeEnabled();
      expect(screen.getByRole("button", { name: /waiting/i })).toBeDisabled();
    });
    expect(screen.queryByText(/req-1234/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/remaining/i)).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox"), "Next follow-up{enter}");
    expect(screen.getByRole("textbox")).toHaveValue("Next follow-up");
    const inputPostCount = requestMock.mock.calls.filter(
      ([path, init]) =>
        String(path).endsWith("/input") &&
        (init as RequestInit | undefined)?.method === "POST",
    ).length;
    expect(inputPostCount).toBe(1);
  });

  it("replays the same ID automatically after a runtime-draining refusal with no receipt", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/shared/api/base");
    const requestIds: string[] = [];

    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        requestIds.push(payload.client_request_id);
        if (requestIds.length === 1) {
          return Promise.reject(
            new ApiError({
              url: String(path),
              status: 503,
              body: {
                detail: {
                  error_code: "runtime_draining",
                  message: "Runtime is restarting.",
                },
              },
            }),
          );
        }
        return Promise.resolve({
          outcome: "sent",
          input_id: 7,
          intent: payload.intent,
          client_request_id: payload.client_request_id,
          queued: [],
        });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({ chatMode: "managed_local", timelineItems: [] });

    await user.type(screen.getByRole("textbox"), "Continue locally");
    await user.click(screen.getByRole("button", { name: /send/i }));

    // A restart is not a failure: no "Not confirmed" and no Retry click;
    // the same operation is re-sent on its own once the runtime is back.
    expect(
      screen.queryByText("Not confirmed — retry with the same request"),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(requestIds).toHaveLength(2), { timeout: 3_000 });
    expect(requestIds[1]).toBe(requestIds[0]);
    await waitFor(() =>
      expect(
        screen.queryByText("Continue locally", {
          selector: "span.session-chat-pending-message__text",
        }),
      ).not.toBeInTheDocument(),
    );
    const stored = JSON.parse(
      window.localStorage.getItem(
        `longhouse:session-input:sess-1:${requestIds[0]}`,
      ) ?? "null",
    );
    expect(stored).toMatchObject({ deliveryConfirmed: true, attachments: [] });
  });
  it("queues a send during an update and sends it under the stored ID when serving resumes", async () => {
    const user = userEvent.setup();
    const postedRequestIds: string[] = [];
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).includes("/inputs?client_request_id=")) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/inputs") && !init?.method) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        postedRequestIds.push(payload.client_request_id);
        return Promise.resolve({
          outcome: "sent",
          input_id: 7,
          intent: payload.intent,
          client_request_id: payload.client_request_id,
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    const now = Date.now();
    hostLinkStore.observeLifecycle({
      state: "updating",
      runtime_epoch: "runtime-old",
      attempt_id: "attempt-1",
      expected_back_by: new Date(now + 30_000).toISOString(),
      deadline: new Date(now + 60_000).toISOString(),
      cutoff: new Date(now + 90_000).toISOString(),
    });
    renderSessionChat({ chatMode: "managed_local", timelineItems: [] });

    await user.type(screen.getByRole("textbox"), "wait for the update");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByTestId("session-chat-update-queued")).toHaveTextContent(
      HOST_LINK_COPY.sendQueued,
    );
    expect(postedRequestIds).toHaveLength(0);
    const storedKey = Array.from({ length: window.localStorage.length }, (_, index) =>
      window.localStorage.key(index),
    ).find((key) => key?.startsWith("longhouse:session-input:sess-1:"));
    expect(storedKey).toBeTruthy();
    const stored = JSON.parse(window.localStorage.getItem(storedKey!) ?? "null");

    act(() =>
      hostLinkStore.observeLifecycle({
        state: "serving",
        runtime_epoch: "runtime-new",
      }),
    );
    await waitFor(() => expect(postedRequestIds).toEqual([stored.clientRequestId]));
    expect(screen.queryByText("Not delivered")).not.toBeInTheDocument();
  });

  it("does not mark a K1 runtime_restarting refusal failed and retries with its ID", async () => {
    const user = userEvent.setup();
    const postedRequestIds: string[] = [];
    const now = Date.now();
    const lifecycle = {
      type: "host.lifecycle" as const,
      state: "updating" as const,
      runtime_epoch: "runtime-old",
      attempt_id: "attempt-1",
      phase: "drain",
      expected_back_by: new Date(now + 30_000).toISOString(),
      deadline: new Date(now + 60_000).toISOString(),
      cutoff: new Date(now + 90_000).toISOString(),
    };
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).includes("/inputs?client_request_id=")) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/inputs") && !init?.method) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        postedRequestIds.push(payload.client_request_id);
        if (postedRequestIds.length === 1) {
          const body = {
            code: "runtime_restarting",
            retryable: true,
            runtime_epoch: "runtime-old",
            admission: "draining",
            claim: lifecycle,
          };
          hostLinkStore.observeApiError(503, body);
          return Promise.reject(
            new ApiError({ url: String(path), status: 503, body }),
          );
        }
        return Promise.resolve({
          outcome: "sent",
          input_id: 8,
          intent: payload.intent,
          client_request_id: payload.client_request_id,
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
    const firstView = renderSessionChat({ chatMode: "managed_local", timelineItems: [] });

    await user.type(screen.getByRole("textbox"), "restart raced the send");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByTestId("session-chat-update-queued")).toHaveTextContent(
      HOST_LINK_COPY.sendQueued,
    );
    expect(postedRequestIds).toHaveLength(1);
    expect(screen.queryByText("Not delivered")).not.toBeInTheDocument();
    const requestId = postedRequestIds[0];

    const storedKey = Array.from({ length: window.localStorage.length }, (_, index) =>
      window.localStorage.key(index),
    ).find((key) => key?.startsWith("longhouse:session-input:sess-1:"));
    const stored = JSON.parse(window.localStorage.getItem(storedKey!) ?? "null");
    expect(stored).toMatchObject({
      clientRequestId: requestId,
      waitingForHostUpdate: true,
    });
    firstView.unmount();
    act(() =>
      hostLinkStore.observeLifecycle({
        state: "serving",
        runtime_epoch: "runtime-new",
      }),
    );
    renderSessionChat({ chatMode: "managed_local", timelineItems: [] });
    await waitFor(() => expect(postedRequestIds).toHaveLength(2));
    expect(postedRequestIds[1]).toBe(requestId);
    expect(screen.queryByText("Not delivered")).not.toBeInTheDocument();
    const delivered = JSON.parse(window.localStorage.getItem(storedKey!) ?? "null");
    expect(delivered).toMatchObject({ deliveryConfirmed: true });
    expect(delivered).not.toHaveProperty("waitingForHostUpdate");
  });
  it("retains provider-ambiguous intent after explicit same-ID replay", async () => {
    const user = userEvent.setup();
    const requestIds: string[] = [];
    const receipts = new Map<
      string,
      { id: null; client_request_id: string; text: string; intent: "auto"; status: "failed"; last_error: string; created_at: null }
    >();
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).includes("/inputs?client_request_id=")) {
        const clientRequestId = new URLSearchParams(
          String(path).split("?")[1] ?? "",
        ).get("client_request_id");
        const receipt = clientRequestId ? receipts.get(clientRequestId) : undefined;
        return Promise.resolve(receipt ? [receipt] : []);
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve(Array.from(receipts.values()));
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        requestIds.push(payload.client_request_id);
        const receipt = {
          id: null,
          client_request_id: payload.client_request_id,
          text: payload.text,
          intent: "auto" as const,
          status: "failed" as const,
          last_error: `${requestIds.length === 1 ? "delivery_unknown" : "provider_unknown"}: provider response not confirmed`,
          created_at: null,
        };
        receipts.set(payload.client_request_id, receipt);
        return Promise.resolve({
          disposition: "accepted",
          outcome: "unknown",
          intent: payload.intent,
          client_request_id: payload.client_request_id,
          queued: [receipt],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({ chatMode: "managed_local", timelineItems: [] });
    await user.type(screen.getByRole("textbox"), "first unresolved");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() =>
      expect(
        screen.getByText("first unresolved", {
          selector: "span.session-chat-pending-message__text",
        }),
      ).toBeInTheDocument(),
    );
    await user.clear(screen.getByRole("textbox"));

    await user.clear(screen.getByRole("textbox"));
    await user.type(screen.getByRole("textbox"), "second unresolved");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() =>
      expect(
        screen.getByText("second unresolved", {
          selector: "span.session-chat-pending-message__text",
        }),
      ).toBeInTheDocument(),
    );
    expect(requestIds).toHaveLength(2);
    expect(requestIds[0]).not.toBe(requestIds[1]);
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(2);

    await user.click(screen.getAllByRole("button", { name: "Retry" })[0]);
    await waitFor(() => expect(requestIds).toHaveLength(3));
    expect(requestIds[2]).toBe(requestIds[0]);
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(2);
  });
  it("hydrates the current session outbox on mount and clears it on switch", async () => {
    const clientRequestId = "web-reload-1";
    window.localStorage.setItem(
      `longhouse:session-input:sess-1:${clientRequestId}`,
      JSON.stringify({
        sessionId: "sess-1",
        text: "survive reload",
        intent: "auto",
        clientRequestId,
        attachments: [],
        createdAt: 1,
      }),
    );
    requestMock.mockImplementation((path: string) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/inputs")) return Promise.resolve([]);
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    const first = renderSessionChat({
      chatMode: "managed_local",
      session: makeSession({ id: "sess-1" }),
    });
    expect(await screen.findByText("survive reload")).toBeInTheDocument();
    first.unmount();

    const second = renderSessionChat({
      chatMode: "managed_local",
      session: makeSession({ id: "sess-1" }),
    });
    expect(await screen.findByText("survive reload")).toBeInTheDocument();
    window.localStorage.setItem(
      "longhouse:session-input:sess-2:web-switch-1",
      JSON.stringify({
        sessionId: "sess-2",
        text: "only for session two",
        intent: "auto",
        clientRequestId: "web-switch-1",
        attachments: [],
        createdAt: 2,
      }),
    );

    second.rerenderSessionChat({
      chatMode: "managed_local",
      session: makeSession({ id: "sess-2" }),
    });
    await waitFor(() =>
      expect(screen.queryByText("survive reload")).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("only for session two")).toBeInTheDocument();
    second.unmount();
  });

  it("keeps other stored sends when one attachment payload is missing", async () => {
    window.localStorage.setItem(
      "longhouse:session-input:sess-1:web-lost-images",
      JSON.stringify({
        sessionId: "sess-1",
        text: "images were here",
        intent: "auto",
        clientRequestId: "web-lost-images",
        attachments: [{ filename: "shot.png", type: "image/png", size: 10 }],
        createdAt: 1,
      }),
    );
    window.localStorage.setItem(
      "longhouse:session-input:sess-1:web-text-only",
      JSON.stringify({
        sessionId: "sess-1",
        text: "plain text survives",
        intent: "auto",
        clientRequestId: "web-text-only",
        attachments: [],
        createdAt: 2,
      }),
    );
    requestMock.mockImplementation((path: string) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/inputs")) return Promise.resolve([]);
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
      session: makeSession({ id: "sess-1" }),
    });

    expect(await screen.findByText("plain text survives")).toBeInTheDocument();
    expect(await screen.findByText("images were here")).toBeInTheDocument();
    expect(
      screen.getByText(/this browser lost its images/),
    ).toBeInTheDocument();
    // Only the text-only row may offer a same-ID retry.
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  });

  it("rejects a send whose request ID already belongs to a different payload", async () => {
    const user = userEvent.setup();
    const onOutboxChange = vi.fn();
    let exactLookupCount = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      const requestPath = String(path);
      if (requestPath.endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (requestPath.includes("/inputs?client_request_id=")) {
        exactLookupCount += 1;
        return Promise.resolve({ id: 7, status: "delivered" });
      }
      if (requestPath.endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (requestPath.endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        return Promise.reject(
          new ApiError({
            url: requestPath,
            status: 409,
            body: {
              detail: {
                error_code: "idempotency_conflict",
                message: "Request ID reused with a different payload.",
                disposition: "accepted",
                delivery_status: "delivered",
                client_request_id: payload.client_request_id,
                live_input_id: "earlier-input",
              },
            },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
      timelineItems: [],
      onOutboxChange,
      session: makeSession({ provider: "codex" }),
    });
    await user.type(screen.getByRole("textbox"), "a different message");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      const calls = onOutboxChange.mock.calls;
      const entries = (calls[calls.length - 1]?.[0] ?? []) as OutboxEntry[];
      expect(entries[0]).toMatchObject({
        text: "a different message",
        state: "failed",
        detail: expect.stringMatching(/already belongs to a different message/),
      });
    });
    // The earlier input's receipt must not stand in for this one.
    expect(exactLookupCount).toBe(0);
  });

  it("keeps runtime-draining refusal retryable with the same operation ID", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/shared/api/base");
    const requestIds: string[] = [];
    let receipt: {
      id: number;
      client_request_id: string;
      text: string;
      intent: "auto";
      status: "delivering" | "delivered";
      last_error: string;
      created_at: null;
    } | null = null;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).includes("/inputs?client_request_id=")) {
        return Promise.resolve(receipt ? [receipt] : []);
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve(receipt ? [receipt] : []);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        requestIds.push(payload.client_request_id);
        if (requestIds.length === 1) {
          receipt = {
            id: 77,
            client_request_id: payload.client_request_id,
            text: payload.text,
            intent: "auto",
            status: "delivering",
            last_error: "runtime_draining: runtime is restarting",
            created_at: null,
          };
          return Promise.reject(
            new ApiError({
              url: String(path),
              status: 503,
              body: {
                detail: {
                  error_code: "runtime_draining",
                  message: "Runtime is restarting.",
                },
              },
            }),
          );
        }
        if (!receipt) {
          return Promise.reject(new Error("Retry had no durable receipt."));
        }
        receipt = { ...receipt, status: "delivered", last_error: "" };
        return Promise.resolve({
          disposition: "accepted",
          outcome: "sent",
          input_id: receipt.id,
          intent: "auto",
          client_request_id: payload.client_request_id,
          queued: [receipt],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({ chatMode: "managed_local", timelineItems: [] });
    await user.type(screen.getByRole("textbox"), "retry after restart");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() =>
      expect(
        screen.getByText("retry after restart", {
          selector: "span.session-chat-pending-message__text",
        }),
      ).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(requestIds).toHaveLength(2));
    expect(requestIds[1]).toBe(requestIds[0]);
    await waitFor(() =>
      expect(
        screen.queryByText("retry after restart", {
          selector: "span.session-chat-pending-message__text",
        }),
      ).not.toBeInTheDocument(),
    );
  });
  it("rehydrates attachment bytes before retrying a draining refusal", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/shared/api/base");
    const requestIds: string[] = [];
    const multipartBodies: FormData[] = [];
    let receipt: {
      id: number;
      client_request_id: string;
      text: string;
      intent: "auto";
      status: "delivering";
      last_error: string;
      created_at: null;
    } | null = null;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve(receipt ? [receipt] : []);
      }
      if (
        String(path).endsWith("/inputs-multipart") &&
        init?.method === "POST"
      ) {
        const form = init.body as FormData;
        multipartBodies.push(form);
        const clientRequestId = String(form.get("client_request_id"));
        requestIds.push(clientRequestId);
        if (requestIds.length === 1) {
          receipt = {
            id: 81,
            client_request_id: clientRequestId,
            text: String(form.get("text") ?? ""),
            intent: "auto",
            status: "delivering",
            last_error: "runtime_draining: runtime is restarting",
            created_at: null,
          };
          return Promise.reject(
            new ApiError({
              url: String(path),
              status: 503,
              body: {
                detail: {
                  error_code: "runtime_draining",
                  message: "Runtime is restarting.",
                },
              },
            }),
          );
        }
        return Promise.resolve({
          outcome: "sent",
          input_id: 82,
          intent: "auto",
          client_request_id: clientRequestId,
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    const session = makeSession({
      provider: "codex",
      capabilities: { attach_images: true },
    });
    const first = renderSessionChat({ chatMode: "managed_local", session });
    const input = first.container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement | null;
    expect(input).toBeTruthy();
    const bytes = [7, 8, 9];
    await user.upload(
      input!,
      new File([new Uint8Array(bytes)], "note.png", { type: "image/png" }),
    );
    await user.click(screen.getByRole("button", { name: /send/i }));
    // Leave before the automatic re-send fires; the remount must recover the
    // stored bytes and offer the same-ID retry itself.
    await waitFor(() => expect(requestIds).toHaveLength(1));
    first.unmount();

    const second = renderSessionChat({ chatMode: "managed_local", session });
    await screen.findByText("Not confirmed — retry with the same request");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(requestIds).toHaveLength(2));
    expect(requestIds[1]).toBe(requestIds[0]);
    const attachment = multipartBodies[1].get("attachments");
    expect(attachment).toBeInstanceOf(File);
    if (!(attachment instanceof File)) throw new Error("Expected attachment file");
    const attachmentBytes = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(attachment);
    });
    expect(
      Array.from(new Uint8Array(attachmentBytes)),
    ).toEqual(bytes);
    second.unmount();
  });
  it("routes attachment-only sends through multipart with empty text", async () => {
    const user = userEvent.setup();
    let jsonCalls = 0;
    let multipartBody: FormData | null = null;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (
        String(path).endsWith("/inputs-multipart") &&
        init?.method === "POST"
      ) {
        multipartBody = init.body as FormData;
        return Promise.resolve({
          outcome: "sent",
          input_id: 9,
          intent: "auto",
          client_request_id: (multipartBody as FormData).get(
            "client_request_id",
          ),
          queued: [],
        });
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        jsonCalls += 1;
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    const { container } = renderSessionChat({
      chatMode: "managed_local",
      session: makeSession({
        provider: "codex",
        capabilities: { attach_images: true },
      }),
    });

    const input = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement | null;
    expect(input).toBeTruthy();
    const file = new File([new Uint8Array([1, 2, 3])], "shot.png", {
      type: "image/png",
    });
    await user.upload(input!, file);

    expect(await screen.findByAltText("shot.png")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(multipartBody).not.toBeNull());
    expect(jsonCalls).toBe(0);
    expect(multipartBody?.get("text")).toBe("");
    expect(multipartBody?.get("intent")).toBe("auto");
    expect(multipartBody?.get("attachments")).toBeInstanceOf(File);
    await waitFor(() => expect(screen.getByText("Sent")).toBeInTheDocument());
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
        const payload = JSON.parse(String(init.body ?? "{}"));
        return Promise.resolve({
          outcome: "sent",
          input_id: inputCalls,
          intent: "auto",
          client_request_id: payload.client_request_id,
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

    expect(
      screen.getByTestId("session-chat-explicit-submit-hint"),
    ).toHaveTextContent("Click send to confirm.");
    expect(inputCalls).toBe(0);

    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(inputCalls).toBe(1));
  });

  it("queues an auto send when the session is locked and capability is on", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(
      ["session-lock", "sess-1"],
      {
        locked: true,
        holder: null,
        time_remaining_seconds: null,
        fork_available: true,
      },
    );

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
        const payload = JSON.parse(String(init.body ?? "{}"));
        return Promise.resolve({
          outcome: "queued",
          input_id: 42,
          intent: "auto",
          client_request_id: payload.client_request_id,
          queued: [
            {
              id: 42,
              client_request_id: payload.client_request_id,
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

    expect(screen.getByRole("button", { name: /stop/i })).toBeEnabled();

    await user.type(screen.getByRole("textbox"), "wait for it");
    // Button says "Queue next" while working with queue capability, and is
    // enabled once a draft exists.
    const queueButton = await screen.findByRole("button", {
      name: /queue next/i,
    });
    expect(queueButton).toBeEnabled();
    await user.click(queueButton);

    const chip = await screen.findByTestId("session-chat-queued");
    expect(chip).toHaveTextContent("wait for it");
    expect(chip).toHaveTextContent(/queued/i);
    expect(
      screen.getByRole("button", { name: /cancel queued message/i }),
    ).toBeEnabled();
  });

  it("shows an uncertain Console dispatch instead of leaving it silently sending", async () => {
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([
          {
            id: 43,
            text: "survive the reconnect",
            intent: "auto",
            status: "delivering",
            created_at: null,
            last_error:
              "turn_start_outcome_unknown: Machine control channel disconnected",
          },
        ]);
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({ chatMode: "managed_local", canQueueNextInput: true });

    const chip = await screen.findByTestId("session-chat-queued");
    expect(chip).toHaveTextContent("survive the reconnect");
    expect(chip).toHaveTextContent(
      "turn_start_outcome_unknown: Machine control channel disconnected",
    );
    expect(chip).not.toHaveTextContent("Sending…");
  });

  it("shows a persisted Console launch failure on the input instead of a request banner", async () => {
    const user = userEvent.setup();
    const onOutboxChange = vi.fn();
    let failedReceipt:
      | {
          id: null;
          live_input_id: string;
          client_request_id: string;
          text: string;
          intent: "auto";
          status: "failed";
          created_at: null;
          last_error: string;
        }
      | null = null;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      const requestPath = String(path);
      if (requestPath.endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (requestPath.includes("/inputs?client_request_id=")) {
        return Promise.resolve(failedReceipt ? [failedReceipt] : []);
      }
      if (requestPath.endsWith("/inputs") && !init) {
        return Promise.resolve(failedReceipt ? [failedReceipt] : []);
      }
      if (requestPath.endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        failedReceipt = {
          id: null,
          live_input_id: "failed-console-input",
          client_request_id: payload.client_request_id,
          text: payload.text,
          intent: "auto",
          status: "failed",
          created_at: null,
          last_error: "cwd_not_found: cwd does not exist: /missing",
        };
        return Promise.reject(
          new ApiError({
            url: requestPath,
            status: 502,
            body: {
              detail: {
                error_code: "cwd_not_found",
                message: "cwd does not exist: /missing",
                disposition: "accepted",
                delivery_status: "failed",
                client_request_id: payload.client_request_id,
                live_input_id: "failed-console-input",
              },
            },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
      canQueueNextInput: true,
      onOutboxChange,
    });

    await user.type(screen.getByRole("textbox"), "launch from missing cwd");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() => {
      const calls = onOutboxChange.mock.calls;
      const entries = (calls[calls.length - 1]?.[0] ?? []) as OutboxEntry[];
      expect(entries[0]).toMatchObject({
        text: "launch from missing cwd",
        state: "failed",
      });
      expect(entries[0].detail).toContain("cwd_not_found");
      expect(entries[0].actions?.map((action) => action.label)).toEqual([
        "Edit",
        "Discard",
      ]);
    });
    expect(screen.queryByText("Request failed (502)")).not.toBeInTheDocument();
  });

  it("keeps an accepted terminal Console failure when exact receipt lookup fails", async () => {
    const user = userEvent.setup();
    const onOutboxChange = vi.fn();
    let requestId = "";
    let exactLookupCount = 0;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      const requestPath = String(path);
      if (requestPath.endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (requestPath.includes("/inputs?client_request_id=")) {
        exactLookupCount += 1;
        return Promise.reject(new Error("Exact receipt lookup unavailable."));
      }
      if (requestPath.endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (requestPath.endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        requestId = payload.client_request_id;
        return Promise.reject(
          new ApiError({
            url: requestPath,
            status: 502,
            body: {
              detail: {
                error_code: "provider_launch_failed",
                message: "Provider did not start.",
                disposition: "accepted",
                delivery_status: "failed",
                client_request_id: requestId,
                live_input_id: "failed-console-input",
              },
            },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({
      chatMode: "managed_local",
      timelineItems: [],
      onOutboxChange,
      session: makeSession({ provider: "codex" }),
    });
    await user.type(screen.getByRole("textbox"), "keep the accepted failure");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      const calls = onOutboxChange.mock.calls;
      const entries = (calls[calls.length - 1]?.[0] ?? []) as OutboxEntry[];
      expect(exactLookupCount).toBe(1);
      expect(entries[0]).toMatchObject({
        text: "keep the accepted failure",
        state: "failed",
        detail: "Provider did not start.",
        actions: [{ label: "Edit" }, { label: "Discard" }],
      });
    });
    expect(requestId).not.toBe("");
    expect(screen.queryByText(/Not confirmed —/)).not.toBeInTheDocument();
  });

  it("loads a durable input failure after the Console session has ended", async () => {
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([
          {
            id: null,
            live_input_id: "ended-console-failure",
            text: "launch from missing cwd",
            intent: "auto",
            status: "failed",
            created_at: null,
            last_error: "cwd_not_found: cwd does not exist: /missing",
          },
        ]);
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat({ chatMode: "managed_local", canQueueNextInput: false });

    const failure = await screen.findByTestId("session-chat-queued-failed");
    expect(failure).toHaveTextContent("launch from missing cwd");
    expect(failure).toHaveTextContent(
      "cwd_not_found: cwd does not exist: /missing",
    );
  });

  it("shows Send update primary + Queue next secondary when steer capability is on", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(
      ["session-lock", "sess-1"],
      {
        locked: true,
        holder: null,
        time_remaining_seconds: null,
        fork_available: true,
      },
    );

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
            client_request_id: payload.client_request_id,
            queued: [],
          });
        }
        if (payload.intent === "queue") {
          queueCalls += 1;
          return Promise.resolve({
            outcome: "queued",
            input_id: 100 + queueCalls,
            intent: "queue",
            client_request_id: payload.client_request_id,
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

  it("uses runtime execution as working state when the lock endpoint is stale false", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(
      ["session-lock", "sess-1"],
      {
        locked: false,
        holder: null,
        time_remaining_seconds: null,
        fork_available: false,
      },
    );

    let submittedIntent: string | null = null;
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: false, fork_available: false });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        submittedIntent = payload.intent;
        return Promise.resolve({
          outcome: "sent",
          input_id: 44,
          intent: "steer",
          client_request_id: payload.client_request_id,
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat(
      {
        chatMode: "managed_local",
        canQueueNextInput: true,
        canSteerActiveTurn: true,
        session: makeSession({
          session_state: makeSessionStateFacts({
            activity: "executing",
            access: "live_control",
            interruptAvailable: true,
          }),
        }),
      },
      { queryClient },
    );

    await user.type(screen.getByRole("textbox"), "still working");
    expect(screen.getByRole("button", { name: /send update/i })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: /send update/i }));

    await waitFor(() => expect(submittedIntent).toBe("steer"));
  });

  it("clears accepted steer input without waiting for a durable user transcript row", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(
      ["session-lock", "sess-1"],
      {
        locked: true,
        holder: null,
        time_remaining_seconds: null,
        fork_available: true,
      },
    );

    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      if (String(path).endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (String(path).endsWith("/inputs") && !init) {
        return Promise.resolve([]);
      }
      if (String(path).endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        return Promise.resolve({
          outcome: "sent",
          input_id: 45,
          intent: "steer",
          client_request_id: payload.client_request_id,
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat(
      {
        chatMode: "managed_local",
        canQueueNextInput: true,
        canSteerActiveTurn: true,
        session: makeSession({
          session_state: makeSessionStateFacts({
            activity: "executing",
            access: "live_control",
            interruptAvailable: true,
          }),
        }),
        timelineItems: [],
      },
      { queryClient },
    );

    await user.type(screen.getByRole("textbox"), "redirect now");
    await user.click(screen.getByRole("button", { name: /send update/i }));

    await waitFor(() => {
      expect(screen.queryByText("redirect now")).not.toBeInTheDocument();
      expect(screen.queryByText("Delivering...")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Sent")).toBeInTheDocument();
  });

  it("offers Queue instead after a steer fails with turn_ended", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(
      ["session-lock", "sess-1"],
      {
        locked: true,
        holder: null,
        time_remaining_seconds: null,
        fork_available: true,
      },
    );

    const { ApiError } = await import("@/shared/api/base");

    const requestIds: string[] = [];
    let queueCalls = 0;
    const serverReceipts = new Map<
      string,
      {
        id: number;
        client_request_id: string;
        text: string;
        intent: string;
        status: string;
        last_error?: string | null;
        created_at: null;
      }
    >();
    requestMock.mockImplementation((path: string, init?: RequestInit) => {
      const requestPath = String(path);
      if (requestPath.endsWith("/lock")) {
        return Promise.resolve({ locked: true, fork_available: true });
      }
      if (requestPath.includes("/inputs?client_request_id=")) {
        const clientRequestId = new URLSearchParams(
          requestPath.split("?")[1] ?? "",
        ).get("client_request_id");
        const receipt = clientRequestId
          ? serverReceipts.get(clientRequestId)
          : undefined;
        return Promise.resolve(receipt ? [receipt] : []);
      }
      if (requestPath.endsWith("/inputs") && !init) {
        return Promise.resolve([...serverReceipts.values()]);
      }
      if (requestPath.endsWith("/input") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}"));
        requestIds.push(payload.client_request_id);
        if (payload.intent === "steer") {
          const receipt = {
            id: 199,
            client_request_id: payload.client_request_id,
            text: payload.text,
            intent: "steer",
            status: "failed",
            last_error: "turn_ended: The active turn already ended.",
            created_at: null,
          };
          serverReceipts.set(receipt.client_request_id, receipt);
          return Promise.reject(
            new ApiError({
              url: requestPath,
              status: 409,
              body: {
                detail: {
                  error_code: "turn_ended",
                  message: "The active turn already ended.",
                  disposition: "accepted",
                  delivery_status: "failed",
                  client_request_id: receipt.client_request_id,
                  input_id: receipt.id,
                },
              },
            }),
          );
        }
        if (payload.intent === "queue") {
          queueCalls += 1;
          const receipt = {
            id: 200 + queueCalls,
            client_request_id: payload.client_request_id,
            text: payload.text,
            intent: "queue",
            status: "queued",
            created_at: null,
          };
          serverReceipts.set(receipt.client_request_id, receipt);
          return Promise.resolve({
            disposition: "accepted",
            outcome: "queued",
            input_id: receipt.id,
            intent: "queue",
            client_request_id: receipt.client_request_id,
            queued: [receipt],
          });
        }
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    const view = renderSessionChat(
      {
        chatMode: "managed_local",
        canQueueNextInput: true,
        canSteerActiveTurn: true,
      },
      { queryClient },
    );

    await user.type(screen.getByRole("textbox"), "too late");
    await user.click(screen.getByRole("button", { name: /send update/i }));

    const prompt = await screen.findByTestId("session-chat-turn-ended");
    expect(prompt).toHaveTextContent("too late");
    const originalKey = `longhouse:session-input:sess-1:${requestIds[0]}`;
    expect(window.localStorage.getItem(originalKey)).not.toBeNull();

    await user.click(screen.getByRole("button", { name: /queue instead/i }));
    await waitFor(() => {
      expect(queueCalls).toBe(1);
      expect(requestIds).toHaveLength(2);
      expect(screen.queryByTestId("session-chat-turn-ended")).not.toBeInTheDocument();
      const replacementKey = `longhouse:session-input:sess-1:${requestIds[1]}`;
      expect(window.localStorage.getItem(replacementKey)).not.toBeNull();
      expect(window.localStorage.getItem(originalKey)).toBeNull();
    });
    expect(requestIds[1]).not.toBe(requestIds[0]);

    await act(async () => {
      await view.queryClient.invalidateQueries({
        queryKey: ["session-inputs", "sess-1"],
      });
    });
    expect(screen.queryByTestId("session-chat-queued-failed")).not.toBeInTheDocument();
  });


  it("does not silently queue when Enter is pressed while working", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(
      ["session-lock", "sess-1"],
      {
        locked: true,
        holder: null,
        time_remaining_seconds: null,
        fork_available: true,
      },
    );

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
        const payload = JSON.parse(String(init.body ?? "{}"));
        return Promise.resolve({
          outcome: "queued",
          input_id: postCalls,
          intent: "auto",
          client_request_id: payload.client_request_id,
          queued: [],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSessionChat(
      { chatMode: "managed_local", canQueueNextInput: true },
      { queryClient },
    );

    await user.type(
      screen.getByRole("textbox"),
      "do not silently queue{enter}",
    );
    expect(postCalls).toBe(0);
    expect(screen.getByRole("textbox")).toHaveValue("do not silently queue");
  });

  describe("with the outbox in the transcript", () => {
    function lastOutbox(mock: ReturnType<typeof vi.fn>): OutboxEntry[] {
      const calls = mock.mock.calls;
      return (calls[calls.length - 1]?.[0] ?? []) as OutboxEntry[];
    }

    function mockSendOutcome(outcome: "sent" | "queued", text: string) {
      const serverInputs = new Map<
        string,
        {
          id: number;
          client_request_id: string;
          text: string;
          intent: string;
          status: string;
          created_at: null;
        }
      >();
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (requestPath.includes("/inputs?client_request_id=")) {
          const clientRequestId = new URLSearchParams(
            requestPath.split("?")[1] ?? "",
          ).get("client_request_id");
          const receipt = clientRequestId
            ? serverInputs.get(clientRequestId)
            : undefined;
          return Promise.resolve(receipt ? [receipt] : []);
        }
        if (requestPath.endsWith("/inputs") && !init) {
          return Promise.resolve([...serverInputs.values()]);
        }
        if (requestPath.endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}"));
          const row = {
            id: 7,
            client_request_id: payload.client_request_id as string,
            text,
            intent: "auto",
            status: outcome === "sent" ? "delivered" : "queued",
            created_at: null,
          };
          serverInputs.set(row.client_request_id, row);
          return Promise.resolve({
            disposition: "accepted",
            outcome,
            input_id: 7,
            intent: "auto",
            client_request_id: row.client_request_id,
            queued: [row],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
    }




    it("matches a delivered send by the server input id alone", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      mockSendOutcome("sent", "by id");
      const { rerenderSessionChat } = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });

      await user.type(screen.getByRole("textbox"), "by id");
      await user.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { state: "sent" },
        ]),
      );

      rerenderSessionChat({
        chatMode: "managed_local",
        onOutboxChange,
        timelineItems: [
          makeLonghouseUserItem({ sessionInputId: 7, text: "reworded" }),
        ],
      });
      await waitFor(() => expect(lastOutbox(onOutboxChange)).toEqual([]));
    });

    it("shows a rehydrated send the server is delivering as sending", async () => {
      const onOutboxChange = vi.fn();
      window.localStorage.setItem(
        "longhouse:session-input:sess-1:web-draining-1",
        JSON.stringify({
          sessionId: "sess-1",
          text: "on its way",
          intent: "auto",
          clientRequestId: "web-draining-1",
          attachments: [],
          createdAt: 1,
        }),
      );
      requestMock.mockImplementation((path: string) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/inputs")) {
          return Promise.resolve([
            {
              id: 3,
              client_request_id: "web-draining-1",
              text: "on its way",
              intent: "auto",
              status: "delivering",
              created_at: null,
            },
          ]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });

      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { text: "on its way", state: "sending" },
        ]),
      );
      expect(lastOutbox(onOutboxChange)[0].actions).toBeUndefined();
    });

    it("keeps a cancelled queued operation editable until explicitly discarded", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      let cancelled = false;
      let clientRequestId = "";
      const receipt = () => ({
        id: 7,
        client_request_id: clientRequestId,
        text: "never mind",
        intent: "auto",
        status: cancelled ? "cancelled" : "queued",
        last_error: cancelled ? "cancelled by user" : null,
        created_at: null,
      });
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (requestPath.includes("/inputs?client_request_id=")) {
          return Promise.resolve([receipt()]);
        }
        if (requestPath.endsWith("/inputs") && !init) {
          return Promise.resolve(cancelled ? [] : [receipt()]);
        }
        if (init?.method === "DELETE") {
          cancelled = true;
          return Promise.resolve({ cancelled: true, input_id: 7 });
        }
        if (requestPath.endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}"));
          clientRequestId = payload.client_request_id;
          return Promise.resolve({
            disposition: "accepted",
            outcome: "queued",
            input_id: 7,
            intent: "auto",
            client_request_id: clientRequestId,
            queued: [receipt()],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });

      await user.type(screen.getByRole("textbox"), "never mind");
      await user.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)[0]?.actions?.[0]?.label).toBe(
          "Cancel",
        ),
      );

      lastOutbox(onOutboxChange)[0].actions?.[0].onClick();
      await waitFor(() => {
        const [entry] = lastOutbox(onOutboxChange);
        expect(entry).toMatchObject({ text: "never mind", state: "failed" });
        expect(entry.actions?.map((action) => action.label)).toEqual([
          "Edit",
          "Discard",
        ]);
      });
      expect(cancelled).toBe(true);
      const outboxKey = `longhouse:session-input:sess-1:${clientRequestId}`;
      expect(window.localStorage.getItem(outboxKey)).not.toBeNull();

      lastOutbox(onOutboxChange)[0].actions?.[1]?.onClick();
      await waitFor(() => expect(lastOutbox(onOutboxChange)).toEqual([]));
      expect(window.localStorage.getItem(outboxKey)).toBeNull();
    });

    it("keeps a dismissed recent failed receipt hidden after remount", async () => {
      const clientRequestId = "server-failed-1";
      const onOutboxChange = vi.fn();
      const failedReceipt = {
        id: 44,
        client_request_id: clientRequestId,
        text: "discard this failed input",
        intent: "auto",
        status: "failed",
        last_error: "provider_launch_failed: process did not start",
        created_at: null,
      };
      requestMock.mockImplementation((path: string) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/inputs")) {
          return Promise.resolve([failedReceipt]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const firstView = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });
      await waitFor(() => {
        const [entry] = lastOutbox(onOutboxChange);
        expect(entry).toMatchObject({
          text: "discard this failed input",
          state: "failed",
        });
        expect(entry.actions?.map((action) => action.label)).toEqual([
          "Dismiss",
        ]);
      });

      lastOutbox(onOutboxChange)[0].actions?.[0].onClick();
      await waitFor(() => expect(lastOutbox(onOutboxChange)).toEqual([]));
      firstView.unmount();

      const previousCallCount = requestMock.mock.calls.length;
      renderSessionChat({ chatMode: "managed_local", timelineItems: [] });
      await waitFor(() =>
        expect(
          requestMock.mock.calls
            .slice(previousCallCount)
            .some(([path]) => String(path).endsWith("/inputs")),
        ).toBe(true),
      );
      expect(
        screen.queryByTestId("session-chat-queued-failed"),
      ).not.toBeInTheDocument();
    });

    it("restores the stored model when editing a failed operation", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      const clientRequestId = "failed-edit-model";
      const savedModel = "provider-model-at-send";
      const failedReceipt = {
        id: 31,
        client_request_id: clientRequestId,
        text: "keep this model",
        intent: "auto",
        status: "failed",
        last_error: "provider_launch_failed: provider did not start",
        created_at: null,
      };
      const requestBodies: Record<string, unknown>[] = [];
      window.localStorage.setItem(
        `longhouse:session-input:sess-1:${clientRequestId}`,
        JSON.stringify({
          sessionId: "sess-1",
          text: "keep this model",
          intent: "auto",
          clientRequestId,
          model: savedModel,
          attachments: [],
          createdAt: 1,
        }),
      );
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (requestPath.includes("/inputs?client_request_id=")) {
          const requestedId = new URLSearchParams(
            requestPath.split("?")[1] ?? "",
          ).get("client_request_id");
          return Promise.resolve(
            requestedId === clientRequestId ? [failedReceipt] : [],
          );
        }
        if (requestPath.endsWith("/inputs") && !init) {
          return Promise.resolve([failedReceipt]);
        }
        if (requestPath.endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}")) as Record<
            string,
            unknown
          >;
          requestBodies.push(payload);
          return Promise.resolve({
            disposition: "accepted",
            outcome: "sent",
            input_id: 32,
            intent: "auto",
            client_request_id: payload.client_request_id,
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const view = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
        session: makeSession({ selected_model: "current-picker-model" }),
      });
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { text: "keep this model", state: "failed" },
        ]),
      );
      act(() => lastOutbox(onOutboxChange)[0].actions?.[0].onClick());
      expect(screen.getByRole("textbox")).toHaveValue("keep this model");
      await user.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() => expect(requestBodies).toHaveLength(1));
      expect(requestBodies[0].model).toBe(savedModel);
      expect(requestBodies[0].client_request_id).not.toBe(clientRequestId);

      await act(async () => {
        await view.queryClient.invalidateQueries({
          queryKey: ["session-inputs", "sess-1"],
        });
      });
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { text: "keep this model", state: "sent" },
        ]),
      );
      expect(lastOutbox(onOutboxChange)).toHaveLength(1);
    });

    it("replaces the failed operation being edited when Queue next sends", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      const clientRequestId = "failed-queue-next";
      const text = "queue this instruction";
      const failedReceipt = {
        id: 31,
        live_input_id: "failed-live-queue",
        client_request_id: clientRequestId,
        text,
        intent: "auto",
        status: "failed",
        last_error: "provider_launch_failed: provider did not start",
        created_at: null,
      };
      const requestIds: string[] = [];
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      queryClient.setQueryData<SessionLockInfo | null>(
        ["session-lock", "sess-1"],
        {
          locked: true,
          holder: null,
          time_remaining_seconds: null,
          fork_available: true,
        },
      );
      const failedKey = `longhouse:session-input:sess-1:${clientRequestId}`;
      window.localStorage.setItem(
        failedKey,
        JSON.stringify({
          sessionId: "sess-1",
          text,
          intent: "auto",
          clientRequestId,
          model: "codex-model-at-send",
          attachments: [],
          createdAt: 1,
        }),
      );
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked: true, fork_available: true });
        }
        if (requestPath.includes("/inputs?client_request_id=")) {
          const requestedId = new URLSearchParams(
            requestPath.split("?")[1] ?? "",
          ).get("client_request_id");
          return Promise.resolve(
            requestedId === clientRequestId ? [failedReceipt] : [],
          );
        }
        if (requestPath.endsWith("/inputs") && !init) {
          return Promise.resolve([failedReceipt]);
        }
        if (requestPath.endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}"));
          requestIds.push(payload.client_request_id);
          return Promise.resolve({
            disposition: "accepted",
            outcome: "queued",
            input_id: 32,
            client_request_id: payload.client_request_id,
            intent: "queue",
            queued: [
              {
                id: 32,
                client_request_id: payload.client_request_id,
                text: payload.text,
                intent: "queue",
                status: "queued",
                created_at: null,
              },
            ],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      renderSessionChat({
        chatMode: "managed_local",
        canSteerActiveTurn: true,
        canQueueNextInput: true,
        timelineItems: [],
        onOutboxChange,
        session: makeSession({ provider: "codex" }),
      }, { queryClient });
      await waitFor(() => {
        const [entry] = lastOutbox(onOutboxChange);
        expect(entry).toMatchObject({ text, state: "failed" });
        expect(entry.actions?.map((action) => action.label)).toEqual([
          "Edit",
          "Discard",
        ]);
      });

      act(() => {
        lastOutbox(onOutboxChange)[0].actions
          ?.find((action) => action.label === "Edit")
          ?.onClick();
      });
      expect(screen.getByRole("textbox")).toHaveValue(text);
      await user.click(screen.getByRole("button", { name: /queue next/i }));

      await waitFor(() => expect(requestIds).toHaveLength(1));
      const replacementId = requestIds[0];
      expect(replacementId).not.toBe(clientRequestId);
      expect(window.localStorage.getItem(failedKey)).toBeNull();
      expect(
        window.localStorage.getItem(
          `longhouse:session-input:sess-1:${replacementId}`,
        ),
      ).not.toBeNull();
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { text, state: "queued" },
        ]),
      );
      expect(lastOutbox(onOutboxChange)).toHaveLength(1);
    });

    it("keeps an edited image intact when Queue next cannot carry it", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      const text = "include this image";
      let failedReceipt:
        | {
            id: number;
            live_input_id: string;
            client_request_id: string;
            text: string;
            intent: "auto";
            status: "failed";
            last_error: string;
            created_at: null;
          }
        | null = null;
      let multipartPosts = 0;
      let textPosts = 0;
      let clientRequestId = "";
      let locked = false;
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      queryClient.setQueryData<SessionLockInfo | null>(
        ["session-lock", "sess-1"],
        {
          locked: false,
          holder: null,
          time_remaining_seconds: null,
          fork_available: true,
        },
      );
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked, fork_available: true });
        }
        if (requestPath.includes("/inputs?client_request_id=")) {
          return Promise.resolve(failedReceipt ? [failedReceipt] : []);
        }
        if (requestPath.endsWith("/inputs") && !init) {
          return Promise.resolve(failedReceipt ? [failedReceipt] : []);
        }
        if (
          requestPath.endsWith("/inputs-multipart") &&
          init?.method === "POST"
        ) {
          multipartPosts += 1;
          const form = init.body as FormData;
          clientRequestId = String(form.get("client_request_id"));
          failedReceipt = {
            id: 44,
            live_input_id: "failed-image-input",
            client_request_id: clientRequestId,
            text: String(form.get("text") ?? ""),
            intent: "auto",
            status: "failed",
            last_error: "provider_launch_failed: provider did not start",
            created_at: null,
          };
          return Promise.reject(
            new ApiError({
              url: requestPath,
              status: 502,
              body: {
                detail: {
                  error_code: "provider_launch_failed",
                  message: "provider did not start",
                  disposition: "accepted",
                  delivery_status: "failed",
                  client_request_id: clientRequestId,
                  live_input_id: "failed-image-input",
                },
              },
            }),
          );
        }
        if (requestPath.endsWith("/input") && init?.method === "POST") {
          textPosts += 1;
          return Promise.resolve({
            disposition: "accepted",
            outcome: "queued",
            intent: "queue",
            client_request_id: "unexpected-queue",
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const view = renderSessionChat(
        {
          chatMode: "managed_local",
          canQueueNextInput: true,
          canSteerActiveTurn: true,
          timelineItems: [],
          onOutboxChange,
          session: makeSession({
            provider: "codex",
            capabilities: { attach_images: true },
          }),
        },
        { queryClient },
      );
      const input = view.container.querySelector(
        'input[type="file"]',
      ) as HTMLInputElement | null;
      expect(input).toBeTruthy();
      await user.upload(
        input!,
        new File([new Uint8Array([1, 2, 3])], "support.png", {
          type: "image/png",
        }),
      );
      await user.type(screen.getByRole("textbox"), text);
      await user.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { text, state: "failed" },
        ]),
      );

      act(() => {
        lastOutbox(onOutboxChange)[0].actions
          ?.find((action) => action.label === "Edit")
          ?.onClick();
      });
      expect(screen.getByRole("textbox")).toHaveValue(text);
      expect(await screen.findByAltText("support.png")).toBeInTheDocument();
      locked = true;
      await act(async () => {
        queryClient.setQueryData<SessionLockInfo | null>(
          ["session-lock", "sess-1"],
          {
            locked: true,
            holder: null,
            time_remaining_seconds: null,
            fork_available: true,
          },
        );
      });
      await screen.findByRole("button", { name: /queue next/i });
      await user.click(screen.getByRole("button", { name: /queue next/i }));

      expect(
        await screen.findByText(
          "Image attachments can only be sent when the session is ready for a new turn.",
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("textbox")).toHaveValue(text);
      expect(screen.getByAltText("support.png")).toBeInTheDocument();
      expect(multipartPosts).toBe(1);
      expect(textPosts).toBe(0);
      expect(
        window.localStorage.getItem(
          `longhouse:session-input:sess-1:${clientRequestId}`,
        ),
      ).not.toBeNull();
    });

    it("reports a queued send with a cancel action", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      mockSendOutcome("queued", "after this");
      renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });

      await user.type(screen.getByRole("textbox"), "after this");
      await user.click(screen.getByRole("button", { name: /send/i }));

      await waitFor(() => {
        const [entry] = lastOutbox(onOutboxChange);
        expect(entry).toMatchObject({ text: "after this", state: "queued" });
        expect(entry.actions?.map((action) => action.label)).toEqual([
          "Cancel",
        ]);
      });
      expect(
        screen.queryByTestId("session-chat-queued"),
      ).not.toBeInTheDocument();
    });
    it("retains Console input bytes until the exact turn completes", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      let exactState = "starting";
      let clientRequestId = "";
      const receipt = () => ({
        id: null,
        live_input_id: "turn-1",
        client_request_id: clientRequestId,
        text: "keep this Console turn",
        intent: "auto",
        status: "delivered",
        turn: {
          turn_id: "turn-1",
          run_id: "run-1",
          state: exactState,
          is_fresh: true,
        },
        created_at: null,
      });
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (requestPath.includes("/inputs?client_request_id=")) {
          return Promise.resolve([receipt()]);
        }
        if (requestPath.endsWith("/inputs") && !init) {
          return Promise.resolve([]);
        }
        if (requestPath.endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}"));
          clientRequestId = payload.client_request_id;
          return Promise.resolve({
            disposition: "accepted",
            outcome: "sent",
            input_id: null,
            live_input_id: "turn-1",
            intent: "auto",
            client_request_id: clientRequestId,
            turn: { turn_id: "turn-1", run_id: "run-1", state: "starting", is_fresh: true },
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const view = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });
      await user.type(screen.getByRole("textbox"), "keep this Console turn");
      await user.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { state: "sent", text: "keep this Console turn" },
        ]),
      );
      const outboxKey = `longhouse:session-input:sess-1:${clientRequestId}`;
      expect(window.localStorage.getItem(outboxKey)).not.toBeNull();

      exactState = "completed";
      await act(async () => {
        await view.queryClient.invalidateQueries({
          queryKey: ["session-input", "sess-1", clientRequestId],
        });
      });
      await waitFor(() => {
        expect(JSON.parse(window.localStorage.getItem(outboxKey) ?? "null")).toMatchObject({
          deliveryConfirmed: true,
          attachments: [],
        });
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { state: "sent", text: "keep this Console turn" },
        ]);
      });
    });

    it("preserves wake origin on server-owned transcript rows", async () => {
      const onOutboxChange = vi.fn();
      const wakeText = "Background task finished: the branch is ready";
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/inputs") && !init) {
          return Promise.resolve([
            {
              id: 81,
              live_input_id: "wake-input-1",
              client_request_id: "wake:invocation-1:1",
              text: wakeText,
              intent: "auto",
              status: "delivered",
              turn: {
                turn_id: "wake-turn-1",
                run_id: "wake-run-1",
                state: "active",
                origin: "wake",
                is_fresh: true,
              },
              created_at: "2026-04-15T16:11:55Z",
            },
          ]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const view = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
        session: makeSession(),
      });
      try {
        await waitFor(() =>
          expect(lastOutbox(onOutboxChange)).toMatchObject([
            { origin: "wake", text: wakeText, state: "sent" },
          ]),
        );
      } finally {
        view.unmount();
        view.queryClient.clear();
      }
    });

    it("polls a remote active Console turn until it terminates", async () => {
      const onOutboxChange = vi.fn();
      const clientRequestId = "remote-console-turn";
      let turnState = "active";
      let listRequests = 0;
      const listedTurnStates: string[] = [];
      const receipt = () => ({
        id: 71,
        client_request_id: clientRequestId,
        text: "",
        intent: "auto",
        status: "delivered",
        turn: {
          turn_id: "remote-turn-1",
          run_id: "remote-run-1",
          state: turnState,
          is_fresh: true,
        },
        created_at: null,
        attachments: [
          { filename: "reference.png", mime_type: "image/png", byte_size: 123 },
        ],
      });
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (requestPath.endsWith("/inputs") && !init) {
          listRequests += 1;
          const currentReceipt = receipt();
          listedTurnStates.push(currentReceipt.turn.state);
          return Promise.resolve([currentReceipt]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const view = renderSessionChat(
        {
          chatMode: "managed_local",
          timelineItems: [],
          onOutboxChange,
          session: makeSession({ provider: "codex" }),
        },
        { queryClient },
      );
      try {
        await waitFor(
          () =>
            expect(lastOutbox(onOutboxChange)).toMatchObject([
              {
                state: "sent",
                text: "",
                attachments: [
                  {
                    filename: "reference.png",
                    mimeType: "image/png",
                    byteSize: 123,
                  },
                ],
              },
            ]),
          { timeout: 3_000 },
        );
        const activeRows = queryClient.getQueryData(["session-inputs", "sess-1"]);
        const activeOutboxUpdates = onOutboxChange.mock.calls.length;
        expect(listRequests).toBe(1);
        turnState = "completed";
        await waitFor(() => expect(listRequests).toBe(2), {
          timeout: 5_000,
        });
        const completedRows = queryClient.getQueryData(["session-inputs", "sess-1"]);
        expect(completedRows).not.toBe(activeRows);
        expect(onOutboxChange.mock.calls.length).toBeGreaterThan(
          activeOutboxUpdates,
        );
        expect(listedTurnStates).toEqual(["active", "completed"]);
        expect(queryClient.getQueryData(["session-inputs", "sess-1"])).toMatchObject([
          { turn: { state: "completed" } },
        ]);
        await waitFor(() => expect(lastOutbox(onOutboxChange)).toEqual([]), {
          timeout: 1_000,
        });
      } finally {
        view.unmount();
        queryClient.clear();
      }
    });

    it("keeps a remote stale Console receipt unconfirmed without polling", async () => {
      const onOutboxChange = vi.fn();
      let listRequests = 0;
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/inputs") && !init) {
          listRequests += 1;
          return Promise.resolve([
            {
              id: 71,
              client_request_id: "stale-console-turn",
              text: "stale remote input",
              intent: "auto",
              status: "delivered",
              turn: {
                turn_id: "stale-turn-1",
                run_id: "stale-run-1",
                state: "active",
                is_fresh: false,
              },
              created_at: null,
              attachments: [],
            },
          ]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const view = renderSessionChat(
        {
          chatMode: "managed_local",
          timelineItems: [],
          onOutboxChange,
          session: makeSession({ provider: "codex" }),
        },
        { queryClient },
      );
      try {
        await waitFor(() =>
          expect(lastOutbox(onOutboxChange)).toMatchObject([
            {
              state: "unconfirmed",
              text: "stale remote input",
              detail: "Console activity is stale; current delivery status is unconfirmed.",
            },
          ]),
        );
        await waitForDuration(2_100);
        expect(listRequests).toBe(1);
        expect(lastOutbox(onOutboxChange)[0]?.state).toBe("unconfirmed");
      } finally {
        view.unmount();
        queryClient.clear();
      }
    });

    it("never shows a transcript-linked receipt in the outbox, whatever its turn says", async () => {
      const onOutboxChange = vi.fn();
      // A run-end that never arrived leaves turns active or queued and stale;
      // the server links the transcript event for delivering receipts too.
      const linked = (id: number, status: string, state: string) => ({
        id,
        client_request_id: `linked-${id}`,
        text: `answered input ${id}`,
        intent: "auto",
        status,
        durable_event_id: `event-${id}`,
        turn: { turn_id: `turn-${id}`, run_id: `run-${id}`, state, is_fresh: false },
        created_at: null,
        attachments: [],
      });
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/inputs") && !init) {
          return Promise.resolve([
            linked(72, "delivered", "active"),
            linked(73, "delivered", "queued"),
            linked(74, "delivering", "active"),
          ]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const view = renderSessionChat(
        {
          chatMode: "managed_local",
          timelineItems: [],
          onOutboxChange,
          session: makeSession({ provider: "codex" }),
        },
        { queryClient },
      );
      try {
        await waitFor(() =>
          expect(requestMock.mock.calls.some(([p]) => String(p).endsWith("/inputs"))).toBe(true),
        );
        await waitForDuration(300);
        expect(lastOutbox(onOutboxChange)).toEqual([]);
      } finally {
        view.unmount();
        queryClient.clear();
      }
    });

    it("settles this browser's send once its receipt is in the transcript, even if the turn never ends", async () => {
      const onOutboxChange = vi.fn();
      let linked = false;
      let clientRequestId = "";
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).includes("/inputs?client_request_id=")) {
          return Promise.resolve([
            {
              id: 81,
              client_request_id: clientRequestId,
              text: "start the turn",
              intent: "auto",
              status: "delivered",
              ...(linked ? { durable_event_id: "event-81" } : {}),
              turn: {
                turn_id: "turn-81",
                run_id: "run-81",
                state: "active",
                is_fresh: !linked,
              },
              created_at: null,
            },
          ]);
        }
        if (String(path).endsWith("/inputs") && !init) return Promise.resolve([]);
        if (String(path).endsWith("/input") && init?.method === "POST") {
          clientRequestId = JSON.parse(String(init.body ?? "{}")).client_request_id;
          return Promise.resolve({
            disposition: "accepted",
            outcome: "sent",
            input_id: 81,
            intent: "auto",
            client_request_id: clientRequestId,
            turn: { turn_id: "turn-81", run_id: "run-81", state: "active", is_fresh: true },
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      const view = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "start the turn" },
      });
      fireEvent.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() => {
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { state: "sent", text: "start the turn" },
        ]);
      });

      // The transcript has the message; the run-end never arrived, so the
      // turn reads active and stale. That is not "Not confirmed".
      linked = true;
      await act(async () => {
        await view.queryClient.invalidateQueries({
          queryKey: ["session-input", "sess-1", clientRequestId],
        });
      });
      await waitFor(() => {
        expect(lastOutbox(onOutboxChange)).toEqual([]);
      });
      expect(
        window.localStorage.getItem(`longhouse:session-input:sess-1:${clientRequestId}`),
      ).toBeNull();
    });

    it("shows no delivery banner for transcript-linked receipts when the outbox is not in the transcript", async () => {
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/inputs") && !init) {
          return Promise.resolve([
            {
              id: 73,
              client_request_id: "linked-delivering",
              text: "linked while delivering",
              intent: "auto",
              status: "delivering",
              last_error: "provider_delivery_unknown: lost the ack",
              durable_event_id: "a1",
              turn: { turn_id: "t1", run_id: "r1", state: "active", is_fresh: false },
              created_at: null,
              attachments: [],
            },
            {
              id: 74,
              client_request_id: "linked-failed",
              text: "linked then failed",
              intent: "auto",
              status: "failed",
              last_error: "provider exited",
              durable_event_id: "a2",
              turn: { turn_id: "t2", run_id: "r2", state: "failed", is_fresh: false },
              created_at: null,
              attachments: [],
            },
          ]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const view = renderSessionChat(
        {
          chatMode: "managed_local",
          timelineItems: [],
          session: makeSession({ provider: "codex" }),
        },
        { queryClient },
      );
      try {
        await waitFor(() =>
          expect(requestMock.mock.calls.some(([p]) => String(p).endsWith("/inputs"))).toBe(true),
        );
        await waitForDuration(300);
        expect(screen.queryByTestId("session-chat-queued")).not.toBeInTheDocument();
        expect(screen.queryByTestId("session-chat-queued-failed")).not.toBeInTheDocument();
      } finally {
        view.unmount();
        queryClient.clear();
      }
    });

    it("keeps a delivered Helm summary until transcript echo across reload", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      let multipartBody: FormData | null = null;
      let linkedReceipt: Record<string, unknown> | null = null;
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        const requestPath = String(path);
        if (requestPath.endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (requestPath.includes("/inputs?client_request_id=")) {
          return Promise.resolve(linkedReceipt ? [linkedReceipt] : []);
        }
        if (requestPath.endsWith("/inputs") && !init) {
          return Promise.resolve([]);
        }
        if (
          requestPath.endsWith("/inputs-multipart") &&
          init?.method === "POST"
        ) {
          multipartBody = init.body as FormData;
          return Promise.resolve({
            disposition: "accepted",
            outcome: "sent",
            input_id: 44,
            intent: "auto",
            client_request_id: (multipartBody as FormData).get(
              "client_request_id",
            ),
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      const session = makeSession({
        provider: "codex",
        capabilities: { attach_images: true },
        session_state: makeSessionStateFacts({
          access: "live_control",
          mode: "console",
        }),
      });
      const first = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
        session,
      });
      const input = first.container.querySelector(
        'input[type="file"]',
      ) as HTMLInputElement | null;
      expect(input).toBeTruthy();
      await user.upload(
        input!,
        new File([new Uint8Array([1, 2, 3])], "reference.png", {
          type: "image/png",
        }),
      );
      await user.type(
        screen.getByRole("textbox", { name: "Next instruction" }),
        "no transcript echo yet",
      );
      await user.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          {
            state: "sent",
            text: "no transcript echo yet",
            attachments: [
              { filename: "reference.png", mimeType: "image/png", byteSize: 3 },
            ],
          },
        ]),
      );

      const clientRequestId = String(multipartBody?.get("client_request_id"));
      const key = `longhouse:session-input:sess-1:${clientRequestId}`;
      const stored = JSON.parse(window.localStorage.getItem(key) ?? "null");
      expect(stored).toMatchObject({
        deliveryConfirmed: true,
        attachments: [{ filename: "reference.png", type: "image/png", size: 3 }],
      });

      const databaseOpen = Promise.withResolvers<IDBDatabase>();
      const openRequest = window.indexedDB.open("longhouse-input-outbox", 1);
      openRequest.onsuccess = () => databaseOpen.resolve(openRequest.result);
      openRequest.onerror = () =>
        databaseOpen.reject(openRequest.error ?? new Error("IDB open failed"));
      const database = await databaseOpen.promise;
      const payloadRead = Promise.withResolvers<unknown>();
      const payloadRequest = database
        .transaction("payloads", "readonly")
        .objectStore("payloads")
        .get(key);
      payloadRequest.onsuccess = () => payloadRead.resolve(payloadRequest.result);
      payloadRequest.onerror = () =>
        payloadRead.reject(payloadRequest.error ?? new Error("IDB read failed"));
      expect(await payloadRead.promise).toBeUndefined();
      if (typeof database.close === "function") database.close();
      await waitForDuration(2_100);
      expect(lastOutbox(onOutboxChange)[0]?.state).toBe("sent");
      first.unmount();

      const reopened = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
        session,
      });
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          {
            state: "sent",
            text: "no transcript echo yet",
            attachments: [
              { filename: "reference.png", mimeType: "image/png", byteSize: 3 },
            ],
          },
        ]),
      );
      expect(window.localStorage.getItem(key)).not.toBeNull();
      linkedReceipt = {
        id: 44,
        client_request_id: clientRequestId,
        text: "no transcript echo yet",
        intent: "auto",
        status: "delivered",
        durable_event_id: "event-44",
        attachments: [
          { filename: "reference.png", mime_type: "image/png", byte_size: 3 },
        ],
      };
      await act(async () => {
        await reopened.queryClient.invalidateQueries({
          queryKey: ["session-input", "sess-1", clientRequestId],
        });
      });
      await waitFor(() => {
        expect(window.localStorage.getItem(key)).toBeNull();
        expect(lastOutbox(onOutboxChange)).toEqual([]);
      });
      reopened.unmount();
    });

    it("keeps an active Console send owned by the turn and settles later by exact ID", async () => {
      const onOutboxChange = vi.fn();
      let exactState = "active";
      let clientRequestId = "";
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).includes("/inputs?client_request_id=")) {
          return Promise.resolve([
            {
              id: null,
              live_input_id: "turn-active",
              client_request_id: clientRequestId,
              text: "keep bytes",
              intent: "auto",
              status: "delivered",
              turn: {
                turn_id: "turn-active",
                run_id: "run-active",
                state: exactState,
                is_fresh: true,
              },
              created_at: null,
            },
          ]);
        }
        if (String(path).endsWith("/inputs") && !init) return Promise.resolve([]);
        if (String(path).endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}"));
          clientRequestId = payload.client_request_id;
          return Promise.resolve({
            disposition: "accepted",
            outcome: "sent",
            input_id: null,
            intent: "auto",
            client_request_id: clientRequestId,
            turn: {
              turn_id: "turn-active",
              run_id: "run-active",
              state: "active",
              is_fresh: true,
            },
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      const view = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "keep bytes" },
      });
      fireEvent.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() => {
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { state: "sent", text: "keep bytes" },
        ]);
      });
      expect(
        window.localStorage.getItem(
          `longhouse:session-input:sess-1:${clientRequestId}`,
        ),
      ).not.toBeNull();

      exactState = "failed";
      await act(async () => {
        await view.queryClient.invalidateQueries({
          queryKey: ["session-input", "sess-1", clientRequestId],
        });
      });
      await waitFor(() => {
        expect(lastOutbox(onOutboxChange)).toMatchObject([
          { state: "failed", text: "keep bytes" },
        ]);
      });
      expect(lastOutbox(onOutboxChange)[0].actions?.map((a) => a.label)).toEqual([
        "Edit",
        "Discard",
      ]);
      expect(screen.queryByText("Sent")).not.toBeInTheDocument();
    });

    it("keeps a same-text identity-less transcript row from clearing another operation", async () => {
      const onOutboxChange = vi.fn();
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}"));
          return Promise.resolve({
            disposition: "accepted",
            outcome: "sent",
            input_id: 91,
            intent: "auto",
            client_request_id: payload.client_request_id,
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      const view = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "same operation text" },
      });
      fireEvent.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)).toMatchObject([{ state: "sent" }]),
      );
      view.rerenderSessionChat({
        chatMode: "managed_local",
        timelineItems: [
          makeLonghouseUserItem({
            text: "same operation text",
            authoredVia: null,
          }),
        ],
        onOutboxChange,
      });
      expect(lastOutbox(onOutboxChange)).toHaveLength(1);
    });

    it("settles a remotely cancelled queued Console turn by exact ID and keeps Edit bytes", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      let exactState = "queued";
      let clientRequestId = "";
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).includes("/inputs?client_request_id=")) {
          return Promise.resolve([
            {
              id: null,
              live_input_id: "queued-turn",
              client_request_id: clientRequestId,
              text: "cancel remotely",
              intent: "auto",
              status: exactState === "cancelled" ? "cancelled" : "queued",
              turn: {
                turn_id: "queued-turn",
                run_id: "queued-run",
                state: exactState,
                is_fresh: true,
              },
              created_at: null,
            },
          ]);
        }
        if (String(path).endsWith("/inputs") && !init) return Promise.resolve([]);
        if (init?.method === "DELETE") {
          exactState = "cancelled";
          return Promise.resolve({ cancelled: true, live_input_id: "queued-turn" });
        }
        if (String(path).endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}"));
          clientRequestId = payload.client_request_id;
          return Promise.resolve({
            disposition: "accepted",
            outcome: "queued",
            input_id: null,
            intent: "auto",
            client_request_id: clientRequestId,
            turn: {
              turn_id: "queued-turn",
              run_id: "queued-run",
              state: "queued",
              is_fresh: true,
            },
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      const view = renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
      });
      await user.type(screen.getByRole("textbox"), "cancel remotely");
      await user.click(screen.getByRole("button", { name: /send/i }));
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)[0]?.actions?.[0]?.label).toBe("Cancel"),
      );
      await act(async () => {
        lastOutbox(onOutboxChange)[0].actions?.[0]?.onClick();
        await Promise.resolve();
      });
      await act(async () => {
        await view.queryClient.invalidateQueries({
          queryKey: ["session-input", "sess-1", clientRequestId],
        });
      });
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)[0]).toMatchObject({
          state: "failed",
          text: "cancel remotely",
        }),
      );
      expect(lastOutbox(onOutboxChange)[0].actions?.map((a) => a.label)).toEqual([
        "Edit",
        "Discard",
      ]);
      expect(
        window.localStorage.getItem(
          `longhouse:session-input:sess-1:${clientRequestId}`,
        ),
      ).not.toBeNull();
    });

    it("retries a legacy model-less record without applying the current picker model", async () => {
      const user = userEvent.setup();
      const onOutboxChange = vi.fn();
      const clientRequestId = "legacy-no-model";
      const requestBodies: Record<string, unknown>[] = [];
      window.localStorage.setItem(
        `longhouse:session-input:sess-1:${clientRequestId}`,
        JSON.stringify({
          sessionId: "sess-1",
          text: "legacy request",
          intent: "auto",
          clientRequestId,
          attachments: [],
          createdAt: 1,
        }),
      );
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).includes("/inputs?client_request_id=")) {
          return Promise.resolve([]);
        }
        if (String(path).endsWith("/inputs") && !init) return Promise.resolve([]);
        if (String(path).endsWith("/input") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body ?? "{}")) as Record<
            string,
            unknown
          >;
          requestBodies.push(payload);
          return Promise.resolve({
            disposition: "unknown",
            outcome: "unknown",
            intent: "auto",
            client_request_id: payload.client_request_id,
            queued: [],
          });
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });
      renderSessionChat({
        chatMode: "managed_local",
        timelineItems: [],
        onOutboxChange,
        session: makeSession({ selected_model: "current-picker-model" }),
      });
      await waitFor(() =>
        expect(lastOutbox(onOutboxChange)[0]?.actions?.map((a) => a.label)).toEqual([
          "Retry",
        ]),
      );
      lastOutbox(onOutboxChange)[0].actions?.[0].onClick();
      await waitFor(() => expect(requestBodies).toHaveLength(1));
      expect(requestBodies[0]).not.toHaveProperty("model");
      expect(lastOutbox(onOutboxChange)[0].warning).toMatch(/model was not recorded/i);
    });
    it("stabilizes outbox notifications when the parent stores them", async () => {
      requestMock.mockImplementation((path: string, init?: RequestInit) => {
        if (String(path).endsWith("/lock")) {
          return Promise.resolve({ locked: false, fork_available: false });
        }
        if (String(path).endsWith("/inputs") && !init) {
          return Promise.resolve([]);
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      });

      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      function OutboxParent() {
        const [entries, setEntries] = useState<OutboxEntry[]>([]);
        const onOutboxChange = useCallback(
          (next: OutboxEntry[]) => setEntries(next),
          [],
        );
        return (
          <QueryClientProvider client={queryClient}>
            <SessionChat
              session={makeSession()}
              layout="dock"
              chatMode="managed_local"
              onOutboxChange={onOutboxChange}
            />
            <output data-testid="session-outbox-count">{entries.length}</output>
          </QueryClientProvider>
        );
      }

      render(<OutboxParent />);
      await waitFor(() => expect(requestMock).toHaveBeenCalled());
      expect(screen.getByTestId("session-outbox-count")).toHaveTextContent("0");
    });
  });
});

describe("SessionChat composer status", () => {
  function composerHead() {
    return screen.getByTestId("session-chat-composer-head");
  }

  function lockedClient() {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<SessionLockInfo | null>(["session-lock", "sess-1"], {
      locked: true,
      holder: null,
      time_remaining_seconds: null,
      fork_available: true,
    });
    return queryClient;
  }

  it("shows the server's label verbatim while the work claim is fresh", () => {
    const now = Date.now();
    renderSessionChat(
      {
        session: makeSession({
          session_state: makeSessionStateFacts({
            access: "live_control",
            interruptAvailable: true,
            activity: "thinking",
            // A finished tool leaves its name behind; the served label wins.
            tool: "Bash",
            observedAt: new Date(now - 60_000).toISOString(),
            activityValidUntil: new Date(now + 60_000).toISOString(),
          }),
        }),
      },
      { queryClient: lockedClient() },
    );

    expect(composerHead()).toHaveTextContent("Thinking");
    expect(composerHead()).not.toHaveTextContent("Bash");
    expect(composerHead()).not.toHaveTextContent("Working");
  });

  it("folds idle into the placeholder and keeps the composer to one row", () => {
    const lastResultAt = "2026-10-06T02:21:00Z";
    renderSessionChat({
      session: makeSession({
        session_state: makeSessionStateFacts({
          access: "live_control",
          activity: "quiescent",
          lastResultAt,
          observedAt: lastResultAt,
        }),
      }),
      composerHeaderAccessory: <span data-testid="evidence-accessory" />,
    });

    expect(screen.queryByTestId("session-chat-composer-head")).not.toBeInTheDocument();
    const input = screen.getByLabelText("Next instruction");
    expect(input).toHaveAttribute("placeholder", expect.stringMatching(/^Idle since .+ — message to continue$/));
    // The evidence chip rides on the input's own line, so an idle Helm
    // composer is one line tall; the chips row is only for a model picker.
    expect(input.parentElement).toContainElement(screen.getByTestId("evidence-accessory"));
    expect(screen.queryByTestId("session-chat-composer-chips")).not.toBeInTheDocument();
  });

  it("keeps the caller's placeholder on a closed session that still takes input", () => {
    renderSessionChat({
      session: makeSession({
        session_state: makeSessionStateFacts({
          access: "live_control",
          activity: "quiescent",
          closed: true,
          lastResultAt: "2026-10-06T02:21:00Z",
        }),
      }),
      composerPlaceholder: "Message the ended run",
    });

    const input = screen.getByLabelText("Next instruction");
    expect(input).toBeEnabled();
    expect(input).toHaveAttribute("placeholder", "Message the ended run");
  });

  it("demotes an expired work claim instead of repeating the cached label", () => {
    const now = Date.now();
    renderSessionChat(
      {
        session: makeSession({
          session_state: makeSessionStateFacts({
            access: "live_control",
            interruptAvailable: true,
            activity: "executing",
            observedAt: new Date(now - 600_000).toISOString(),
            activityValidUntil: new Date(now - 60_000).toISOString(),
          }),
        }),
      },
      { queryClient: lockedClient() },
    );

    expect(composerHead()).toHaveTextContent("Activity uncertain");
    expect(composerHead()).not.toHaveTextContent("Using Shell");
  });
});
