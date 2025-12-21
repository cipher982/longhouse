import type { AgentSummary, AgentRun } from "../../services/api";
import { formatDateTimeShort } from "./formatters";

export type SortKey = "name" | "status" | "created_at" | "last_run" | "next_run" | "success";

export type SortConfig = {
  key: SortKey;
  ascending: boolean;
};

export type AgentRunsState = Record<number, AgentRun[]>;

const STATUS_ORDER: Record<string, number> = {
  running: 0,
  processing: 1,
  idle: 2,
  error: 3,
};

const STORAGE_KEY_SORT = "dashboard_sort_key";
const STORAGE_KEY_ASC = "dashboard_sort_asc";

export function loadSortConfig(): SortConfig {
  if (typeof window === "undefined") {
    return { key: "name", ascending: true };
  }

  const storedKey = window.localStorage.getItem(STORAGE_KEY_SORT) ?? "name";
  const storedAsc = window.localStorage.getItem(STORAGE_KEY_ASC);

  const keyMap: Record<string, SortKey> = {
    name: "name",
    status: "status",
    created_at: "created_at",
    last_run: "last_run",
    next_run: "next_run",
    success: "success",
  };

  const key = keyMap[storedKey] ?? "name";
  const ascending = storedAsc === null ? true : storedAsc !== "0";
  return { key, ascending };
}

export function persistSortConfig(config: SortConfig) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(STORAGE_KEY_SORT, config.key);
  window.localStorage.setItem(STORAGE_KEY_ASC, config.ascending ? "1" : "0");
}

export function sortAgents(agents: AgentSummary[], runsByAgent: AgentRunsState, sortConfig: SortConfig): AgentSummary[] {
  const sorted = [...agents];
  sorted.sort((left, right) => {
    const comparison = compareAgents(left, right, runsByAgent, sortConfig.key);
    if (comparison !== 0) {
      return sortConfig.ascending ? comparison : -comparison;
    }
    const fallback = left.name.toLowerCase().localeCompare(right.name.toLowerCase());
    return sortConfig.ascending ? fallback : -fallback;
  });
  return sorted;
}

function compareAgents(
  left: AgentSummary,
  right: AgentSummary,
  runsByAgent: AgentRunsState,
  sortKey: SortKey
): number {
  switch (sortKey) {
    case "name":
      return left.name.toLowerCase().localeCompare(right.name.toLowerCase());
    case "status":
      return (STATUS_ORDER[left.status] ?? 99) - (STATUS_ORDER[right.status] ?? 99);
    case "created_at":
      return formatDateTimeShort(left.created_at ?? null).localeCompare(
        formatDateTimeShort(right.created_at ?? null)
      );
    case "last_run":
      return formatDateTimeShort(left.last_run_at ?? null).localeCompare(
        formatDateTimeShort(right.last_run_at ?? null)
      );
    case "next_run":
      return formatDateTimeShort(left.next_run_at ?? null).localeCompare(
        formatDateTimeShort(right.next_run_at ?? null)
      );
    case "success": {
      const leftStats = computeSuccessStats(runsByAgent[left.id]);
      const rightStats = computeSuccessStats(runsByAgent[right.id]);
      if (leftStats.rate === rightStats.rate) {
        return leftStats.count - rightStats.count;
      }
      return leftStats.rate - rightStats.rate;
    }
    default:
      return 0;
  }
}

export function computeSuccessStats(runs?: AgentRun[]): { display: string; rate: number; count: number } {
  if (!runs || runs.length === 0) {
    return { display: "0.0% (0)", rate: 0, count: 0 };
  }

  const successCount = runs.filter((run) => run.status === "success").length;
  const successRate = runs.length === 0 ? 0 : (successCount / runs.length) * 100;
  return {
    display: `${successRate.toFixed(1)}% (${runs.length})`,
    rate: successRate,
    count: runs.length,
  };
}

export function determineLastRunIndicator(runs?: AgentRun[]): boolean | null {
  if (!runs || runs.length === 0) {
    return null;
  }
  const status = runs[0]?.status;
  if (status === "success") {
    return true;
  }
  if (status === "failed") {
    return false;
  }
  return null;
}
