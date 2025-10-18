import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ChatPage from "../ChatPage";
import { ShelfProvider } from "../../lib/useShelfState";
import type { Thread, ThreadMessage } from "../../services/api";

const apiMocks = vi.hoisted(() => ({
  fetchAgent: vi.fn(),
  fetchThreads: vi.fn(),
  fetchThreadMessages: vi.fn(),
  postThreadMessage: vi.fn(),
  runThread: vi.fn(),
  createThread: vi.fn(),
  updateThread: vi.fn(),
  fetchWorkflows: vi.fn(),
  startWorkflowExecution: vi.fn(),
}));

vi.mock("../../services/api", () => apiMocks);

const {
  fetchAgent: mockFetchAgent,
  fetchThreads: mockFetchThreads,
  fetchThreadMessages: mockFetchThreadMessages,
  postThreadMessage: mockPostThreadMessage,
  runThread: mockRunThread,
  createThread: mockCreateThread,
  updateThread: mockUpdateThread,
  fetchWorkflows: mockFetchWorkflows,
  startWorkflowExecution: mockStartWorkflowExecution,
} = apiMocks;

function renderChatPage(initialEntry = "/chat/1/42") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ShelfProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/chat/:agentId/:threadId" element={<ChatPage />} />
          </Routes>
        </MemoryRouter>
      </ShelfProvider>
    </QueryClientProvider>
  );
}

describe("ChatPage", () => {
  let threadState: Thread;

  beforeEach(() => {
    vi.clearAllMocks();

    const now = new Date().toISOString();
    const thread: Thread = {
      id: 42,
      agent_id: 1,
      title: "Primary",
      agent_state: null,
      memory_strategy: "buffer",
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

    mockFetchAgent.mockResolvedValue({
      id: 1,
      owner_id: 10,
      owner: null,
      name: "Demo Agent",
      status: "running",
      created_at: now,
      updated_at: now,
      model: "gpt-4o",
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
    mockFetchThreads.mockImplementation(() => Promise.resolve([threadState]));
    mockFetchThreadMessages.mockResolvedValue([message]);
    mockPostThreadMessage.mockResolvedValue({ ...message, id: 100, content: "New human message" });
    mockRunThread.mockResolvedValue(undefined);
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

    // Mock workflow functions
    mockFetchWorkflows.mockResolvedValue([
      { id: 1, name: "Test Workflow", description: "A test workflow", canvas: { nodes: [], edges: [] } },
    ]);
    mockStartWorkflowExecution.mockResolvedValue({
      execution_id: 123,
      phase: "running",
      result: null,
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
      expect(mockRunThread).toHaveBeenCalledWith(42);
    });
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
});
