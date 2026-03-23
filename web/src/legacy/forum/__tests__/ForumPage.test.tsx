import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
import ForumPage from "../ForumPage";
import { TestRouter } from "../../../test/test-utils";
import type { ForumActiveSession } from "../api";

vi.mock("../ForumCanvas", () => ({
  ForumCanvas: () => <div data-testid="forum-canvas" />,
}));

vi.mock("../../../components/SessionChat", () => ({
  SessionChat: ({ session }: { session: { id: string } }) => (
    <div data-testid="forum-session-chat">{session.id}</div>
  ),
}));

const useForumSessionsMock = vi.fn();
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    useForumSessions: (options: unknown) => useForumSessionsMock(options),
  };
});

const createTestQueryClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="forum-location">{`${location.pathname}${location.search}`}</div>;
}

function ForumRoute() {
  return (
    <>
      <ForumPage />
      <LocationProbe />
    </>
  );
}

function renderForum(initialEntry = "/forum") {
  const queryClient = createTestQueryClient();

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/forum" element={<ForumRoute />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
}

function makeSession(): ForumActiveSession {
  return {
    id: "session-1",
    project: "longhouse-demo",
    provider: "claude",
    cwd: "/Users/demo/longhouse",
    git_branch: "main",
    started_at: "2026-03-21T12:00:00.000Z",
    ended_at: null,
    last_activity_at: "2026-03-21T12:10:00.000Z",
    status: "working",
    attention: "auto",
    duration_minutes: 12,
    last_user_message: "Scan the repo",
    last_assistant_message: "On it",
    message_count: 3,
    tool_calls: 1,
    presence_state: "thinking",
    presence_tool: "codex",
    user_state: "active",
  };
}

describe("ForumPage", () => {
  afterEach(() => {
    cleanup();
    useForumSessionsMock.mockReset();
  });

  it("shows empty state", async () => {
    useForumSessionsMock.mockReturnValue({
      data: { sessions: [], total: 0, last_refresh: new Date().toISOString() },
      isLoading: false,
      error: null,
    });

    renderForum();

    expect(await screen.findByTestId("forum-canvas")).toBeInTheDocument();
    expect(screen.getByText(/No sessions found/i)).toBeInTheDocument();
  });

  it("reads the selected session directly from the URL", async () => {
    useForumSessionsMock.mockReturnValue({
      data: {
        sessions: [makeSession()],
        total: 1,
        last_refresh: new Date().toISOString(),
      },
      isLoading: false,
      error: null,
    });

    renderForum("/forum?session=session-1");

    expect(await screen.findByTestId("forum-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("forum-location")).toHaveTextContent("/forum?session=session-1");
    expect(screen.getByRole("button", { name: "Chat" })).toBeInTheDocument();
    expect(screen.getAllByText(/Scan the repo/i).length).toBeGreaterThan(0);
  });

  it("reads chat mode directly from the URL", async () => {
    useForumSessionsMock.mockReturnValue({
      data: {
        sessions: [makeSession()],
        total: 1,
        last_refresh: new Date().toISOString(),
      },
      isLoading: false,
      error: null,
    });

    renderForum("/forum?session=session-1&chat=true");

    expect(await screen.findByTestId("forum-session-chat")).toHaveTextContent("session-1");
    expect(screen.getByTestId("forum-location")).toHaveTextContent("/forum?session=session-1&chat=true");
  });

  it("clears invalid session selections from the URL", async () => {
    useForumSessionsMock.mockReturnValue({
      data: {
        sessions: [makeSession()],
        total: 1,
        last_refresh: new Date().toISOString(),
      },
      isLoading: false,
      error: null,
    });

    renderForum("/forum?session=missing");

    await waitFor(() => {
      expect(screen.getByTestId("forum-location")).toHaveTextContent("/forum");
    });
    expect(screen.getByText(/Click any session in the list/i)).toBeInTheDocument();
  });
});
