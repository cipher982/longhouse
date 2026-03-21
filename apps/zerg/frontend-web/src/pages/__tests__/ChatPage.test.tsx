import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TestRouter } from "../../test/test-utils";

import ChatPage from "../ChatPage";
import { ShelfProvider } from "../../lib/useShelfState";
import { ConfirmProvider } from "../../components/confirm";
import type { Thread, ThreadMessage } from "../../services/api";

const apiMocks = vi.hoisted(() => ({
  fetchAutomation: vi.fn(),
  fetchThreads: vi.fn(),
  fetchThreadMessages: vi.fn(),
  postThreadMessage: vi.fn(),
  startThreadRun: vi.fn(),
  createThread: vi.fn(),
  updateThread: vi.fn(),
  fetchContainerPolicy: vi.fn(),
  fetchAccountConnectors: vi.fn().mockResolvedValue([]),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    ...apiMocks,
  };
});

const {
  fetchAutomation: mockFetchAutomation,
  fetchThreads: mockFetchThreads,
  fetchThreadMessages: mockFetchThreadMessages,
  postThreadMessage: mockPostThreadMessage,
  startThreadRun: mockStartThreadRun,
  createThread: mockCreateThread,
  updateThread: mockUpdateThread,
  fetchContainerPolicy: mockFetchContainerPolicy,
} = apiMocks;

function renderChatPage(initialEntry = "/automations/1/thread/42") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ConfirmProvider>
        <ShelfProvider>
          <TestRouter initialEntries={[initialEntry]}>
            <Routes>
              <Route path="/automations/:automationId/thread/:threadId?" element={<ChatPage />} />
              <Route path="/timeline" element={<div>Timeline Home</div>} />
            </Routes>
          </TestRouter>
        </ShelfProvider>
      </ConfirmProvider>
    </QueryClientProvider>
  );
}

describe("ChatPage", () => {
  let threadState: Thread;

  afterEach(() => {
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();

    const now = new Date().toISOString();
    const thread: Thread = {
      id: 42,
      automation_id: 1,
      title: "Primary",
      automation_state: null,
      active: true,
      thread_type: "chat",
      created_at: now,
      updated_at: now,
      messages: [],
    };
    threadState = thread;
    const message: ThreadMessage = {
      id: 99,
      thread_id: 42,
      role: "user",
      content: "Hello from storage",
      timestamp: now,
      processed: true,
    };

    mockFetchAutomation.mockResolvedValue({
      id: 1,
      owner_id: 10,
      owner: null,
      name: "Demo Automation",
      status: "running",
      created_at: now,
      updated_at: now,
      model: "gpt-5.2-chat-latest",
      system_instructions: "",
      task_instructions: "",
      schedule: null,
      config: null,
      last_error: null,
      allowed_tools: [],
      messages: [],
      next_run_at: null,
      last_run_at: null,
    });
    mockFetchThreads.mockImplementation((_automationId: number, threadType?: string) => {
      // Only return the thread for matching thread_type to avoid duplicate keys
      if (threadType === "chat" || threadType === undefined) {
        return Promise.resolve([threadState]);
      }
      return Promise.resolve([]);
    });
    mockFetchThreadMessages.mockResolvedValue([message]);
    mockPostThreadMessage.mockResolvedValue({ ...message, id: 100, content: "New human message" });
    mockStartThreadRun.mockResolvedValue(undefined);
    mockCreateThread.mockResolvedValue({
      ...thread,
      id: 100,
      title: "Generated",
    });
    mockUpdateThread.mockImplementation((_threadId: number, payload: { title?: string | null }) => {
      if (typeof payload.title === "string" && payload.title.trim().length > 0) {
        threadState = { ...threadState, title: payload.title };
      }
      return Promise.resolve(threadState);
    });

    mockFetchContainerPolicy.mockResolvedValue({
      enabled: true,
      default_image: "ubuntu:latest",
      network_enabled: true,
      user_id: 1,
      memory_limit: "1Gi",
      cpus: "1",
      timeout_secs: 300,
      seccomp_profile: null,
    });
  });

  it("renders existing messages and sends a new one", async () => {
    renderChatPage();

    const messages = await screen.findAllByText("Hello from storage");
    expect(messages.length).toBeGreaterThan(0);

    const input = await screen.findByTestId("chat-input");
    const sendButton = await screen.findByTestId("send-message-btn");

    const user = userEvent.setup();
    await user.type(input, "New human message");
    await user.click(sendButton);

    await waitFor(() => {
      expect(mockPostThreadMessage).toHaveBeenCalledWith(42, "New human message");
      expect(mockStartThreadRun).toHaveBeenCalledWith(42);
    });
  });

  it("redirects a threadless route to the most recent chat thread", async () => {
    renderChatPage("/automations/1/thread");

    await waitFor(() => {
      expect(mockFetchThreadMessages).toHaveBeenCalledWith(42);
    });

    const messages = await screen.findAllByText("Hello from storage");
    expect(messages.length).toBeGreaterThan(0);
  });

  it("redirects browser reloads back to the timeline", async () => {
    vi.spyOn(window.performance, "getEntriesByType").mockReturnValue([
      { type: "reload" } as PerformanceNavigationTiming,
    ]);

    renderChatPage();

    expect(await screen.findByText("Timeline Home")).toBeInTheDocument();
  });

  it("shows timeline-focused recovery copy when automation context is missing", async () => {
    renderChatPage("/automations/not-a-number/thread/42");

    expect(await screen.findByText("Missing automation context")).toBeInTheDocument();
    expect(screen.getByText("Open the timeline to pick a session or return to the main app.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to Timeline" })).toBeInTheDocument();
  });

  it("renames a thread and persists via API", async () => {
    renderChatPage();

    const user = userEvent.setup();

    const editButton = await screen.findByTestId("edit-thread-42");
    await user.click(editButton);

    const titleInput = await screen.findByDisplayValue("Primary");
    await user.clear(titleInput);
    await user.type(titleInput, "Renamed{enter}");

    await waitFor(() => {
      expect(mockUpdateThread).toHaveBeenCalledWith(42, { title: "Renamed" });
    });

    await waitFor(() => {
      expect(screen.getByText("Renamed")).toBeInTheDocument();
    });
  });

  it("creates an initial thread for a threadless automation with no chat history", async () => {
    const now = new Date().toISOString();
    const createdThread: Thread = {
      id: 100,
      automation_id: 1,
      title: "Thread 1",
      automation_state: null,
      active: true,
      thread_type: "chat",
      created_at: now,
      updated_at: now,
      messages: [],
    };
    let chatThreads: Thread[] = [];

    mockFetchThreads.mockImplementation((_automationId: number, threadType?: string) => {
      if (threadType === "chat" || threadType === undefined) {
        return Promise.resolve(chatThreads);
      }
      return Promise.resolve([]);
    });
    mockCreateThread.mockImplementation(async () => {
      chatThreads = [createdThread];
      return createdThread;
    });
    mockFetchThreadMessages.mockResolvedValue([]);

    renderChatPage("/automations/1/thread");

    expect(await screen.findByText("Preparing chat...")).toBeInTheDocument();

    await waitFor(() => {
      expect(mockCreateThread).toHaveBeenCalledWith(1, "Thread 1");
    });

    await waitFor(() => {
      expect(mockFetchThreadMessages).toHaveBeenCalledWith(100);
    });
  });
});
