import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TestRouter } from "../../test/test-utils";
import ConversationsPage from "../ConversationsPage";

const apiMocks = vi.hoisted(() => ({
  fetchConversations: vi.fn(),
  searchConversations: vi.fn(),
  fetchConversation: vi.fn(),
  fetchConversationMessages: vi.fn(),
  replyToConversation: vi.fn(),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    ...apiMocks,
  };
});

const {
  fetchConversations: mockFetchConversations,
  searchConversations: mockSearchConversations,
  fetchConversation: mockFetchConversation,
  fetchConversationMessages: mockFetchConversationMessages,
  replyToConversation: mockReplyToConversation,
} = apiMocks;

function renderConversationsPage(initialEntry = "/conversations?conversation=1") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/conversations" element={<ConversationsPage />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>
  );
}

describe("ConversationsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    mockFetchConversations.mockResolvedValue({
      total: 2,
      conversations: [
        {
          id: 1,
          kind: "email",
          title: "Dinner plans",
          status: "active",
          last_message_at: "2026-03-12T18:30:00Z",
          created_at: "2026-03-12T18:00:00Z",
          updated_at: "2026-03-12T18:30:00Z",
          message_count: 2,
          binding_count: 1,
          conversation_metadata: null,
        },
        {
          id: 2,
          kind: "email",
          title: "Portugal planning",
          status: "active",
          last_message_at: "2026-03-12T19:10:00Z",
          created_at: "2026-03-12T18:45:00Z",
          updated_at: "2026-03-12T19:10:00Z",
          message_count: 3,
          binding_count: 1,
          conversation_metadata: null,
        },
      ],
    });
    mockSearchConversations.mockResolvedValue({ total: 0, conversations: [] });
    mockFetchConversation.mockImplementation(async (conversationId: number) => ({
      id: conversationId,
      kind: "email",
      title: conversationId === 1 ? "Dinner plans" : "Portugal planning",
      status: "active",
      last_message_at: "2026-03-12T19:10:00Z",
      created_at: "2026-03-12T18:00:00Z",
      updated_at: "2026-03-12T19:10:00Z",
      message_count: conversationId === 1 ? 2 : 3,
      binding_count: 1,
      conversation_metadata: null,
      bindings: [],
    }));
    mockFetchConversationMessages.mockImplementation(async (conversationId: number) => ({
      total: conversationId === 1 ? 2 : 1,
      messages: conversationId === 1
        ? [
            {
              id: 1,
              conversation_id: 1,
              role: "user",
              direction: "incoming",
              sender_kind: "human",
              sender_display: "friend@example.com",
              content: "Can you book dinner for 7?",
              content_blocks: null,
              external_message_id: "gmail-msg-1",
              parent_message_id: null,
              archive_relpath: null,
              message_metadata: null,
              internal: false,
              sent_at: "2026-03-12T18:30:00Z",
              created_at: "2026-03-12T18:30:00Z",
              updated_at: "2026-03-12T18:30:00Z",
            },
            {
              id: 2,
              conversation_id: 1,
              role: "assistant",
              direction: "outgoing",
              sender_kind: "agent",
              sender_display: "Oikos",
              content: "Booked for 7pm.",
              content_blocks: null,
              external_message_id: "gmail-msg-2",
              parent_message_id: null,
              archive_relpath: null,
              message_metadata: null,
              internal: false,
              sent_at: "2026-03-12T18:35:00Z",
              created_at: "2026-03-12T18:35:00Z",
              updated_at: "2026-03-12T18:35:00Z",
            },
          ]
        : [
            {
              id: 3,
              conversation_id: 2,
              role: "user",
              direction: "incoming",
              sender_kind: "human",
              sender_display: "travel@example.com",
              content: "Can you check flights?",
              content_blocks: null,
              external_message_id: "gmail-msg-3",
              parent_message_id: null,
              archive_relpath: null,
              message_metadata: null,
              internal: false,
              sent_at: "2026-03-12T19:10:00Z",
              created_at: "2026-03-12T19:10:00Z",
              updated_at: "2026-03-12T19:10:00Z",
            },
          ],
    }));
    mockReplyToConversation.mockResolvedValue({
      conversation_id: 2,
      provider: "gmail",
      thread_id: "thread-2",
      subject: "Re: Portugal planning",
      reply_all: true,
      to_emails: ["travel@example.com"],
      cc_emails: ["team@example.com"],
      message: {
        id: 4,
        conversation_id: 2,
        role: "user",
        direction: "outgoing",
        sender_kind: "human",
        sender_display: "owner@gmail.com",
        content: "Check TAP and Delta.",
        content_blocks: null,
        external_message_id: "gmail-msg-4",
        parent_message_id: null,
        archive_relpath: null,
        message_metadata: null,
        internal: false,
        sent_at: "2026-03-12T19:20:00Z",
        created_at: "2026-03-12T19:20:00Z",
        updated_at: "2026-03-12T19:20:00Z",
      },
    });
  });

  it("loads a selected thread and sends a reply", async () => {
    renderConversationsPage();

    expect(await screen.findByTestId("conversation-item-1")).toBeInTheDocument();
    expect(await screen.findByText("Can you book dinner for 7?")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(await screen.findByTestId("conversation-item-2"));

    await waitFor(() => {
      expect(mockFetchConversationMessages).toHaveBeenCalledWith(2, { limit: 200 });
    });

    const textarea = await screen.findByLabelText("Reply");
    await user.type(textarea, "Check TAP and Delta.");
    await user.click(screen.getByLabelText("Reply all"));
    await user.click(screen.getByTestId("conversation-reply-submit"));

    await waitFor(() => {
      expect(mockReplyToConversation).toHaveBeenCalledWith(2, {
        body: "Check TAP and Delta.",
        reply_all: true,
      });
    });
  });
});
