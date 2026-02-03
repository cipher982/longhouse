import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ForumPage from "../ForumPage";
import { TestRouter } from "../../test/test-utils";

vi.mock("../../forum/ForumCanvas", () => ({
  ForumCanvas: () => <div data-testid="forum-canvas" />,
}));

const useActiveSessionsMock = vi.fn();
vi.mock("../../hooks/useActiveSessions", () => ({
  useActiveSessions: (options: unknown) => useActiveSessionsMock(options),
}));

const createTestQueryClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

describe("ForumPage", () => {
  afterEach(() => {
    cleanup();
    useActiveSessionsMock.mockReset();
  });

  it("shows empty state", async () => {
    useActiveSessionsMock.mockReturnValue({
      data: { sessions: [], total: 0, last_refresh: new Date().toISOString() },
      isLoading: false,
      error: null,
    });

    const queryClient = createTestQueryClient();

    render(
      <QueryClientProvider client={queryClient}>
        <TestRouter initialEntries={["/forum"]}>
          <ForumPage />
        </TestRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByTestId("forum-canvas")).toBeInTheDocument();
    expect(screen.getByText(/No sessions found/i)).toBeInTheDocument();
  });

  it("renders sessions in the list", async () => {
    useActiveSessionsMock.mockReturnValue({
      data: {
        sessions: [
          {
            id: "session-1",
            project: "longhouse-demo",
            provider: "claude",
            cwd: "/Users/demo/longhouse",
            git_branch: "main",
            started_at: new Date().toISOString(),
            ended_at: null,
            last_activity_at: new Date().toISOString(),
            status: "working",
            attention: "auto",
            duration_minutes: 12,
            last_user_message: "Scan the repo",
            last_assistant_message: "On it",
            message_count: 3,
            tool_calls: 1,
          },
        ],
        total: 1,
        last_refresh: new Date().toISOString(),
      },
      isLoading: false,
      error: null,
    });

    const queryClient = createTestQueryClient();

    render(
      <QueryClientProvider client={queryClient}>
        <TestRouter initialEntries={["/forum"]}>
          <ForumPage />
        </TestRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByTestId("forum-canvas")).toBeInTheDocument();
    expect(screen.getByText(/Scan the repo/i)).toBeInTheDocument();
  });
});
