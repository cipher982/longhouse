import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { BackgroundTaskRoster, summarizeBackgroundRoster } from "../BackgroundTaskRoster";
import type { SessionStateFacts } from "@/shared/api/agents";

describe("BackgroundTaskRoster", () => {
  it("renders nothing when there are no active delegation items or pending background agents", () => {
    const { container } = render(
      <MemoryRouter>
        <BackgroundTaskRoster delegation={null} pendingBackgroundAgentCount={0} nowMs={Date.now()} />
      </MemoryRouter>,
    );
    expect(container.firstChild).toBeNull();
  });

  it("separates running/waiting agents, shells, and monitors and expands nested subagents with clickable child links", () => {
    const nowMs = Date.parse("2026-04-15T16:12:00Z");
    const delegation: SessionStateFacts["delegation"] = {
      state: "pending",
      count: 4,
      items: [
        {
          id: "agent-root",
          kind: "agent",
          status: "waiting",
          agent_type: "general-purpose",
          description: "Waiting on bench terminal output",
          started_at: "2026-04-15T16:10:00Z",
          session_id: "sess-child-1",
          native_progress: {
            status: "waiting",
            last_intent: "Waiting for gate result",
            tokens: 194_200,
          },
        },
        {
          id: "agent-nested",
          kind: "agent",
          status: "running",
          agent_type: "Explore",
          is_fork: true,
          parent_agent_id: "root",
          spawn_depth: 2,
          description: "Inspect parser subagents loader",
          started_at: "2026-04-15T16:11:00Z",
          session_id: "sess-child-2",
          native_progress: {
            status: "running",
            current_tool: "Grep",
            tokens: 42_500,
            tool_count: 5,
          },
        },
        {
          id: "shell-1",
          kind: "shell",
          status: "running",
          description: "Run cargo test",
          started_at: "2026-04-15T16:11:30Z",
        },
        {
          id: "mon-1",
          kind: "monitor",
          status: "running",
          description: "Watch canary promotion",
          started_at: "2026-04-15T16:11:00Z",
        },
      ],
    };

    expect(summarizeBackgroundRoster(delegation.items!, 2)).toBe(
      "2 agents (1 running, 1 waiting) · 1 shell · 1 monitor",
    );

    render(
      <MemoryRouter>
        <BackgroundTaskRoster delegation={delegation} pendingBackgroundAgentCount={2} nowMs={nowMs} />
      </MemoryRouter>,
    );

    const toggle = screen.getByTestId("session-background-roster-toggle");
    expect(toggle).toHaveTextContent("2 agents (1 running, 1 waiting) · 1 shell · 1 monitor");
    expect(screen.queryByTestId("session-background-roster-list")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    const list = screen.getByTestId("session-background-roster-list");
    const rows = within(list).getAllByTestId("session-background-roster-item");
    expect(rows).toHaveLength(4);
    expect(rows[0]).toHaveAttribute("data-task-status", "waiting");
    expect(rows[0]).toHaveAttribute("data-spawn-depth", "0");
    expect(rows[0]).toHaveTextContent("general-purpose");
    expect(rows[0]).toHaveTextContent("2m");
    expect(rows[0]).toHaveTextContent("194.2k tok");
    expect(rows[1]).toHaveAttribute("data-spawn-depth", "1");
    expect(rows[1]).toHaveTextContent("fork");
    expect(rows[1]).toHaveTextContent("Searching");
    expect(rows[2]).toHaveAttribute("data-task-kind", "shell");
    expect(rows[3]).toHaveAttribute("data-task-kind", "monitor");

    const link = within(rows[0]).getByRole("link", { name: "Waiting on bench terminal output" });
    expect(link).toHaveAttribute("href", "/timeline/sess-child-1");
  });

  it("shows fallback count when only pendingBackgroundAgentCount is present", () => {
    render(
      <MemoryRouter>
        <BackgroundTaskRoster delegation={null} pendingBackgroundAgentCount={3} nowMs={Date.now()} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("session-background-roster")).toHaveTextContent(
      "Waiting for 3 background agents",
    );
  });
});
