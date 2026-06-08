import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkflowRunsPanel } from "../WorkflowRunsPanel";
import { fetchSessionWorkflowRuns, fetchWorkflowRun } from "../../../services/api/agents";

vi.mock("../../../services/api/agents", () => ({
  fetchSessionWorkflowRuns: vi.fn(),
  fetchWorkflowRun: vi.fn(),
}));

const fetchSessionWorkflowRunsMock = vi.mocked(fetchSessionWorkflowRuns);
const fetchWorkflowRunMock = vi.mocked(fetchWorkflowRun);

describe("WorkflowRunsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders one collapsible node per run with skill + agent count, not N rows", async () => {
    fetchSessionWorkflowRunsMock.mockResolvedValue({
      session_id: "parent-1",
      workflow_runs: [{ workflow_run_id: "wf_abc", agent_count: 18, skill: "deep-research" }],
    });

    render(<WorkflowRunsPanel sessionId="parent-1" />);

    // One node, labelled by skill + agent count — not 18 loose rows.
    expect(await screen.findByText("deep-research")).toBeInTheDocument();
    expect(screen.getByText("18 agents")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-wf_abc")).toBeInTheDocument();
    // Agents are not loaded until expanded.
    expect(screen.queryByTestId("workflow-run-agents")).not.toBeInTheDocument();
    expect(fetchWorkflowRunMock).not.toHaveBeenCalled();
  });

  it("drills into the individual agents on expand", async () => {
    fetchSessionWorkflowRunsMock.mockResolvedValue({
      session_id: "parent-1",
      workflow_runs: [{ workflow_run_id: "wf_abc", agent_count: 2, skill: "deep-research" }],
    });
    fetchWorkflowRunMock.mockResolvedValue({
      workflow_run_id: "wf_abc",
      skill: "deep-research",
      parent_session_id: "parent-1",
      agent_count: 2,
      agents: [
        {
          thread_id: "t1",
          session_id: "parent-1",
          is_primary: false,
          branch_kind: "subagent",
          agent_id: "a049eaf15e4dbcae3",
          attribution_agent: "workflow-subagent",
          attribution_skill: "deep-research",
          source_path: null,
        },
        {
          thread_id: "t2",
          session_id: "parent-1",
          is_primary: false,
          branch_kind: "subagent",
          agent_id: "a04eaddc8e3b46986",
          attribution_agent: "workflow-subagent",
          attribution_skill: "deep-research",
          source_path: null,
        },
      ],
    });

    render(<WorkflowRunsPanel sessionId="parent-1" />);
    const node = (await screen.findByTestId("workflow-run-wf_abc")) as HTMLDetailsElement;

    // Open the <details> and fire the native toggle event the component listens for.
    node.open = true;
    fireEvent(node, new Event("toggle", { bubbles: false }));

    await waitFor(() => expect(fetchWorkflowRunMock).toHaveBeenCalledWith("wf_abc"));
    expect(await screen.findByText("a049eaf15e4dbcae3")).toBeInTheDocument();
    expect(screen.getByText("a04eaddc8e3b46986")).toBeInTheDocument();
    expect(screen.getAllByTestId("workflow-run-agent")).toHaveLength(2);
  });

  it("renders nothing when the session has no workflow runs", async () => {
    fetchSessionWorkflowRunsMock.mockResolvedValue({ session_id: "parent-1", workflow_runs: [] });
    const { container } = render(<WorkflowRunsPanel sessionId="parent-1" />);
    await waitFor(() => expect(fetchSessionWorkflowRunsMock).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="session-workflow-runs"]')).toBeNull();
  });
});
