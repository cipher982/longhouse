import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ForumPage from "../ForumPage";
import { TestRouter } from "../../test/test-utils";
import { eventBus } from "../../jarvis/lib/event-bus";

vi.mock("../../forum/ForumCanvas", () => ({
  ForumCanvas: () => <div data-testid="forum-canvas" />,
}));

describe("ForumPage", () => {
  afterEach(() => {
    cleanup();
    eventBus.clear();
  });

  it("renders live events into the task list", async () => {
    const user = userEvent.setup();

    render(
      <TestRouter initialEntries={["/forum"]}>
        <ForumPage />
      </TestRouter>,
    );

    expect(await screen.findByTestId("forum-canvas")).toBeInTheDocument();

    const modeToggle = await screen.findByRole("button", { name: "Replay Mode" });
    await user.click(modeToggle);

    await waitFor(() => {
      expect(screen.getByText("Live")).toBeInTheDocument();
      expect(screen.getByText(/No tasks yet/i)).toBeInTheDocument();
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
