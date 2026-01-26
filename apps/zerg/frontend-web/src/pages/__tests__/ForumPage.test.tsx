import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ForumPage from "../ForumPage";
import { TestRouter } from "../../test/test-utils";
import { eventBus } from "../../jarvis/lib/event-bus";

vi.mock("../../forum/ForumCanvas", () => ({
  ForumCanvas: () => <div data-testid="forum-canvas" />,
}));

vi.mock("../../hooks/useActiveSessions", () => ({
  useActiveSessions: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
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
    eventBus.clear();
  });

  it("renders live events into the task list", async () => {
    const user = userEvent.setup();
    const queryClient = createTestQueryClient();

    render(
      <QueryClientProvider client={queryClient}>
        <TestRouter initialEntries={["/forum"]}>
          <ForumPage />
        </TestRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByTestId("forum-canvas")).toBeInTheDocument();

    const modeToggle = await screen.findByRole("button", { name: "Replay Mode" });
    await user.click(modeToggle);

    await waitFor(() => {
      expect(screen.getByText("Live")).toBeInTheDocument();
      expect(screen.getByText(/No active sessions found/i)).toBeInTheDocument();
    });

    eventBus.emit("supervisor:started", {
      runId: 1,
      task: "Ship logs",
      timestamp: 1000,
    });

    await waitFor(() => {
      expect(screen.getByText("Ship logs")).toBeInTheDocument();
    });

    eventBus.emit("supervisor:worker_spawned", {
      jobId: 7,
      task: "Lint repo",
      timestamp: 1100,
    });

    await waitFor(() => {
      expect(screen.getByText("Lint repo")).toBeInTheDocument();
    });
  });
});
