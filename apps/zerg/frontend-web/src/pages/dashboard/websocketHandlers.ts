import type { DashboardSnapshot, AgentSummary, AgentRun } from "../../services/api";

export function applyAgentStateUpdate(
  current: DashboardSnapshot,
  agentId: number,
  dataPayload: Record<string, unknown>
): DashboardSnapshot {
  const validStatuses = ["idle", "running", "processing", "error"] as const;
  const statusValue =
    typeof dataPayload.status === "string" && validStatuses.includes(dataPayload.status as (typeof validStatuses)[number])
      ? (dataPayload.status as AgentSummary["status"])
      : undefined;
  const lastRunAtValue = typeof dataPayload.last_run_at === "string" ? dataPayload.last_run_at : undefined;
  const nextRunAtValue = typeof dataPayload.next_run_at === "string" ? dataPayload.next_run_at : undefined;
  const lastErrorValue =
    dataPayload.last_error === null || typeof dataPayload.last_error === "string"
      ? (dataPayload.last_error as string | null)
      : undefined;

  let changed = false;
  const nextAgents = current.agents.map((agent) => {
    if (agent.id !== agentId) {
      return agent;
    }

    const nextAgent: AgentSummary = {
      ...agent,
      status: statusValue ?? agent.status,
      last_run_at: lastRunAtValue ?? agent.last_run_at,
      next_run_at: nextRunAtValue ?? agent.next_run_at,
      last_error: lastErrorValue !== undefined ? lastErrorValue : agent.last_error,
    };

    if (
      nextAgent.status !== agent.status ||
      nextAgent.last_run_at !== agent.last_run_at ||
      nextAgent.next_run_at !== agent.next_run_at ||
      nextAgent.last_error !== agent.last_error
    ) {
      changed = true;
      return nextAgent;
    }
    return agent;
  });

  if (!changed) {
    return current;
  }

  return {
    ...current,
    agents: nextAgents,
  };
}

export function applyRunUpdate(
  current: DashboardSnapshot,
  agentId: number,
  dataPayload: Record<string, unknown>
): DashboardSnapshot {
  const runIdCandidate = dataPayload.id ?? dataPayload.run_id;
  const runId = typeof runIdCandidate === "number" ? runIdCandidate : null;
  if (runId == null) {
    return current;
  }

  const threadId =
    typeof dataPayload.thread_id === "number" ? (dataPayload.thread_id as number) : undefined;

  const runsBundles = current.runs.slice();
  let bundleIndex = runsBundles.findIndex((bundle) => bundle.agentId === agentId);
  let runsChanged = false;

  if (bundleIndex === -1) {
    runsBundles.push({ agentId, runs: [] });
    bundleIndex = runsBundles.length - 1;
    runsChanged = true;
  }

  const targetBundle = runsBundles[bundleIndex];
  const existingRuns = targetBundle.runs ?? [];
  const existingIndex = existingRuns.findIndex((run) => run.id === runId);
  let nextRuns = existingRuns;

  if (existingIndex === -1) {
    if (threadId === undefined) {
      return current;
    }

    const newRun: AgentRun = {
      id: runId,
      agent_id: agentId,
      thread_id: threadId,
      status:
        typeof dataPayload.status === "string"
          ? (dataPayload.status as AgentRun["status"])
          : "running",
      trigger:
        typeof dataPayload.trigger === "string"
          ? (dataPayload.trigger as AgentRun["trigger"])
          : "manual",
      started_at: typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : null,
      finished_at: typeof dataPayload.finished_at === "string" ? (dataPayload.finished_at as string) : null,
      duration_ms: typeof dataPayload.duration_ms === "number" ? (dataPayload.duration_ms as number) : null,
      total_tokens: typeof dataPayload.total_tokens === "number" ? (dataPayload.total_tokens as number) : null,
      total_cost_usd:
        typeof dataPayload.total_cost_usd === "number" ? (dataPayload.total_cost_usd as number) : null,
      error:
        dataPayload.error === undefined
          ? null
          : (dataPayload.error as string | null) ?? null,
    };

    nextRuns = [newRun, ...existingRuns];
    if (nextRuns.length > current.runsLimit) {
      nextRuns = nextRuns.slice(0, current.runsLimit);
    }
    runsChanged = true;
  } else {
    const previousRun = existingRuns[existingIndex];
    const updatedRun: AgentRun = {
      ...previousRun,
      status:
        typeof dataPayload.status === "string"
          ? (dataPayload.status as AgentRun["status"])
          : previousRun.status,
      started_at:
        typeof dataPayload.started_at === "string"
          ? (dataPayload.started_at as AgentRun["started_at"])
          : previousRun.started_at,
      finished_at:
        typeof dataPayload.finished_at === "string"
          ? (dataPayload.finished_at as AgentRun["finished_at"])
          : previousRun.finished_at,
      duration_ms:
        typeof dataPayload.duration_ms === "number"
          ? (dataPayload.duration_ms as AgentRun["duration_ms"])
          : previousRun.duration_ms,
      total_tokens:
        typeof dataPayload.total_tokens === "number"
          ? (dataPayload.total_tokens as AgentRun["total_tokens"])
          : previousRun.total_tokens,
      total_cost_usd:
        typeof dataPayload.total_cost_usd === "number"
          ? (dataPayload.total_cost_usd as AgentRun["total_cost_usd"])
          : previousRun.total_cost_usd,
      error:
        dataPayload.error === undefined
          ? previousRun.error
          : ((dataPayload.error as string | null) ?? null),
    };

    const hasRunDiff =
      updatedRun.status !== previousRun.status ||
      updatedRun.started_at !== previousRun.started_at ||
      updatedRun.finished_at !== previousRun.finished_at ||
      updatedRun.duration_ms !== previousRun.duration_ms ||
      updatedRun.total_tokens !== previousRun.total_tokens ||
      updatedRun.total_cost_usd !== previousRun.total_cost_usd ||
      updatedRun.error !== previousRun.error;

    if (hasRunDiff) {
      nextRuns = [...existingRuns];
      nextRuns[existingIndex] = updatedRun;
      runsChanged = true;
    }
  }

  if (runsChanged) {
    runsBundles[bundleIndex] = {
      agentId,
      runs: nextRuns,
    };
  }

  let agentsChanged = false;
  const updatedAgents = current.agents.map((agent) => {
    if (agent.id !== agentId) {
      return agent;
    }

    const statusValue =
      typeof dataPayload.status === "string"
        ? (dataPayload.status as AgentSummary["status"])
        : agent.status;
    const lastRunValue =
      typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : agent.last_run_at;

    if (statusValue === agent.status && lastRunValue === agent.last_run_at) {
      return agent;
    }

    agentsChanged = true;
    return {
      ...agent,
      status: statusValue,
      last_run_at: lastRunValue,
    };
  });

  if (!runsChanged && !agentsChanged) {
    return current;
  }

  return {
    ...current,
    agents: agentsChanged ? updatedAgents : current.agents,
    runs: runsChanged ? runsBundles : current.runs,
  };
}
