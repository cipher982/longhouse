import { useEffect, useState } from "react";

import { Badge } from "../ui";
import {
  fetchSessionWorkflowRuns,
  fetchWorkflowRun,
  type WorkflowRunAgent,
  type WorkflowRunSummary,
} from "../../services/api/agents";

interface WorkflowRunsPanelProps {
  sessionId: string;
}

/**
 * Renders each dynamic-workflow run under a session as ONE collapsible node
 * (skill label + agent count) that drills into its individual subagent threads,
 * instead of scattering N loose subagent rows.
 */
export function WorkflowRunsPanel({ sessionId }: WorkflowRunsPanelProps) {
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);

  useEffect(() => {
    let cancelled = false;
    fetchSessionWorkflowRuns(sessionId)
      .then((res) => {
        if (!cancelled) setRuns(res.workflow_runs);
      })
      .catch(() => {
        if (!cancelled) setRuns([]);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  if (runs.length === 0) return null;

  return (
    <div
      className="session-pane-section session-workflow-runs-panel"
      data-testid="session-workflow-runs"
    >
      <div className="session-pane-section-title">Workflow runs</div>
      {runs.map((run) => (
        <WorkflowRunNode key={run.workflow_run_id} run={run} />
      ))}
    </div>
  );
}

function WorkflowRunNode({ run }: { run: WorkflowRunSummary }) {
  const [agents, setAgents] = useState<WorkflowRunAgent[] | null>(null);
  const [loading, setLoading] = useState(false);

  const onToggle = (event: React.SyntheticEvent<HTMLDetailsElement>) => {
    if (!event.currentTarget.open || agents !== null || loading) return;
    setLoading(true);
    fetchWorkflowRun(run.workflow_run_id)
      .then((res) => setAgents(res.agents))
      .catch(() => setAgents([]))
      .finally(() => setLoading(false));
  };

  const title = run.skill ?? "workflow";
  return (
    <details
      className="session-pane-disclosure session-pane-disclosure--tertiary session-workflow-run"
      data-testid={`workflow-run-${run.workflow_run_id}`}
      onToggle={onToggle}
    >
      <summary className="session-pane-disclosure__summary">
        <span className="session-pane-disclosure__title">{title}</span>
        <span className="session-pane-disclosure__meta">
          <Badge variant="neutral">
            {run.agent_count} {run.agent_count === 1 ? "agent" : "agents"}
          </Badge>
        </span>
      </summary>
      <div className="session-pane-disclosure__body">
        {loading ? <div className="session-workflow-run__loading">Loading agents…</div> : null}
        {agents !== null ? (
          <div className="session-workflow-run__agents" data-testid="workflow-run-agents">
            {agents.map((agent) => (
              <div
                key={agent.thread_id}
                className="session-workflow-run__agent"
                data-testid="workflow-run-agent"
              >
                <span className="session-workflow-run__agent-id">
                  {agent.agent_id ?? agent.thread_id.slice(0, 8)}
                </span>
                {agent.attribution_agent ? (
                  <span className="session-workflow-run__agent-kind">{agent.attribution_agent}</span>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </details>
  );
}
