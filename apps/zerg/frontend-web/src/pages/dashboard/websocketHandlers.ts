import type { DashboardSnapshot, FicheSummary, Run } from "../../services/api";

export function applyFicheStateUpdate(
  current: DashboardSnapshot,
  ficheId: number,
  dataPayload: Record<string, unknown>
): DashboardSnapshot {
  const validStatuses = ["idle", "running", "processing", "error"] as const;
  const statusValue =
    typeof dataPayload.status === "string" && validStatuses.includes(dataPayload.status as (typeof validStatuses)[number])
      ? (dataPayload.status as FicheSummary["status"])
      : undefined;
  const lastRunAtValue = typeof dataPayload.last_run_at === "string" ? dataPayload.last_run_at : undefined;
  const nextRunAtValue = typeof dataPayload.next_run_at === "string" ? dataPayload.next_run_at : undefined;
  const lastErrorValue =
    dataPayload.last_error === null || typeof dataPayload.last_error === "string"
      ? (dataPayload.last_error as string | null)
      : undefined;

  let changed = false;
  const nextFiches = current.fiches.map((fiche) => {
    if (fiche.id !== ficheId) {
      return fiche;
    }

    const nextFiche: FicheSummary = {
      ...fiche,
      status: statusValue ?? fiche.status,
      last_run_at: lastRunAtValue ?? fiche.last_run_at,
      next_run_at: nextRunAtValue ?? fiche.next_run_at,
      last_error: lastErrorValue !== undefined ? lastErrorValue : fiche.last_error,
    };

    if (
      nextFiche.status !== fiche.status ||
      nextFiche.last_run_at !== fiche.last_run_at ||
      nextFiche.next_run_at !== fiche.next_run_at ||
      nextFiche.last_error !== fiche.last_error
    ) {
      changed = true;
      return nextFiche;
    }
    return fiche;
  });

  if (!changed) {
    return current;
  }

  return {
    ...current,
    fiches: nextFiches,
  };
}

export function applyRunUpdate(
  current: DashboardSnapshot,
  ficheId: number,
  dataPayload: Record<string, unknown>
): DashboardSnapshot {
  const runIdCandidate = dataPayload.id ?? dataPayload.run_id;
  const runId = typeof runIdCandidate === "number" ? runIdCandidate : null;
  if (runId == null) {
    return current;
  }

  const threadId =
    typeof dataPayload.thread_id === "number" ? (dataPayload.thread_id as number) : undefined;

  const runBundles = current.runs.slice();
  let bundleIndex = runBundles.findIndex((bundle) => bundle.ficheId === ficheId);
  let runsChanged = false;

  if (bundleIndex === -1) {
    runBundles.push({ ficheId, runs: [] });
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
      fiche_id: ficheId,
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
      ficheId,
      runs: nextRuns,
    };
  }

  let fichesChanged = false;
  const validFicheStatuses = ["idle", "running", "processing", "error"] as const;
  const updatedFiches = current.fiches.map((fiche) => {
    if (fiche.id !== ficheId) {
      return fiche;
    }

    const statusValue =
      typeof dataPayload.status === "string" && validFicheStatuses.includes(dataPayload.status as (typeof validFicheStatuses)[number])
        ? (dataPayload.status as FicheSummary["status"])
        : fiche.status;
    const lastRunValue =
      typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : fiche.last_run_at;

    if (statusValue === fiche.status && lastRunValue === fiche.last_run_at) {
      return fiche;
    }

    fichesChanged = true;
    return {
      ...fiche,
      status: statusValue,
      last_run_at: lastRunValue,
    };
  });

  if (!runsChanged && !fichesChanged) {
    return current;
  }

  return {
    ...current,
    fiches: fichesChanged ? updatedFiches : current.fiches,
    runs: runsChanged ? runBundles : current.runs,
  };
}
