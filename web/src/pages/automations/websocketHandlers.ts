import type { AutomationOverviewSnapshot, AutomationSummary, Run } from "../../services/api";

export function applyAutomationStateUpdate(
  current: AutomationOverviewSnapshot,
  automationId: number,
  dataPayload: Record<string, unknown>
): AutomationOverviewSnapshot {
  const validStatuses = ["idle", "running", "processing", "error"] as const;
  const statusValue =
    typeof dataPayload.status === "string" && validStatuses.includes(dataPayload.status as (typeof validStatuses)[number])
      ? (dataPayload.status as AutomationSummary["status"])
      : undefined;
  const lastRunAtValue = typeof dataPayload.last_run_at === "string" ? dataPayload.last_run_at : undefined;
  const nextRunAtValue = typeof dataPayload.next_run_at === "string" ? dataPayload.next_run_at : undefined;
  const lastErrorValue =
    dataPayload.last_error === null || typeof dataPayload.last_error === "string"
      ? (dataPayload.last_error as string | null)
      : undefined;

  let changed = false;
  const nextAutomations = current.automations.map((automation) => {
    if (automation.id !== automationId) {
      return automation;
    }

    const nextAutomation: AutomationSummary = {
      ...automation,
      status: statusValue ?? automation.status,
      last_run_at: lastRunAtValue ?? automation.last_run_at,
      next_run_at: nextRunAtValue ?? automation.next_run_at,
      last_error: lastErrorValue !== undefined ? lastErrorValue : automation.last_error,
    };

    if (
      nextAutomation.status !== automation.status ||
      nextAutomation.last_run_at !== automation.last_run_at ||
      nextAutomation.next_run_at !== automation.next_run_at ||
      nextAutomation.last_error !== automation.last_error
    ) {
      changed = true;
      return nextAutomation;
    }
    return automation;
  });

  if (!changed) {
    return current;
  }

  return {
    ...current,
    automations: nextAutomations,
  };
}

export function applyRunUpdate(
  current: AutomationOverviewSnapshot,
  automationId: number,
  dataPayload: Record<string, unknown>
): AutomationOverviewSnapshot {
  const runIdCandidate = dataPayload.id ?? dataPayload.run_id;
  const runId = typeof runIdCandidate === "number" ? runIdCandidate : null;
  if (runId == null) {
    return current;
  }

  const threadId =
    typeof dataPayload.thread_id === "number" ? (dataPayload.thread_id as number) : undefined;

  const runBundles = current.runs.slice();
  let bundleIndex = runBundles.findIndex((bundle) => bundle.automationId === automationId);
  let runsChanged = false;

  if (bundleIndex === -1) {
    runBundles.push({ automationId, runs: [] });
    bundleIndex = runBundles.length - 1;
    runsChanged = true;
  }

  const targetBundle = runBundles[bundleIndex];
  const existingRuns = targetBundle.runs ?? [];
  const existingIndex = existingRuns.findIndex((run) => run.id === runId);
  let nextRuns = existingRuns;

  if (existingIndex === -1) {
    if (threadId === undefined) {
      return current;
    }

    const newRun: Run = {
      id: runId,
      automation_id: automationId,
      thread_id: threadId,
      status:
        typeof dataPayload.status === "string"
          ? (dataPayload.status as Run["status"])
          : "running",
      trigger:
        typeof dataPayload.trigger === "string"
          ? (dataPayload.trigger as Run["trigger"])
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
      display_type:
        typeof dataPayload.display_type === "string" ? (dataPayload.display_type as string) : "run",
    };

    nextRuns = [newRun, ...existingRuns];
    if (nextRuns.length > current.runsLimit) {
      nextRuns = nextRuns.slice(0, current.runsLimit);
    }
    runsChanged = true;
  } else {
    const previousRun = existingRuns[existingIndex];
    const updatedRun: Run = {
      ...previousRun,
      status:
        typeof dataPayload.status === "string"
          ? (dataPayload.status as Run["status"])
          : previousRun.status,
      started_at:
        typeof dataPayload.started_at === "string"
          ? (dataPayload.started_at as Run["started_at"])
          : previousRun.started_at,
      finished_at:
        typeof dataPayload.finished_at === "string"
          ? (dataPayload.finished_at as Run["finished_at"])
          : previousRun.finished_at,
      duration_ms:
        typeof dataPayload.duration_ms === "number"
          ? (dataPayload.duration_ms as Run["duration_ms"])
          : previousRun.duration_ms,
      total_tokens:
        typeof dataPayload.total_tokens === "number"
          ? (dataPayload.total_tokens as Run["total_tokens"])
          : previousRun.total_tokens,
      total_cost_usd:
        typeof dataPayload.total_cost_usd === "number"
          ? (dataPayload.total_cost_usd as Run["total_cost_usd"])
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
    runBundles[bundleIndex] = {
      automationId,
      runs: nextRuns,
    };
  }

  let automationsChanged = false;
  const validAutomationStatuses = ["idle", "running", "processing", "error"] as const;
  const updatedAutomations = current.automations.map((automation) => {
    if (automation.id !== automationId) {
      return automation;
    }

    const statusValue =
      typeof dataPayload.status === "string" &&
      validAutomationStatuses.includes(dataPayload.status as (typeof validAutomationStatuses)[number])
        ? (dataPayload.status as AutomationSummary["status"])
        : automation.status;
    const lastRunValue =
      typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : automation.last_run_at;

    if (statusValue === automation.status && lastRunValue === automation.last_run_at) {
      return automation;
    }

    automationsChanged = true;
    return {
      ...automation,
      status: statusValue,
      last_run_at: lastRunValue,
    };
  });

  if (!runsChanged && !automationsChanged) {
    return current;
  }

  return {
    ...current,
    automations: automationsChanged ? updatedAutomations : current.automations,
    runs: runsChanged ? runBundles : current.runs,
  };
}
