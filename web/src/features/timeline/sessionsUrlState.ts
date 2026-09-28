/**
 * Timeline URL state: filters, search and paging read from and written to the query string.
 */

// ---------------------------------------------------------------------------
// URL state helpers
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;
const DEFAULT_DAYS_BACK = 14;
const DEFAULT_SORT_ORDER = "relevant" as const;

export type SortOrder = "relevant" | "recent";

export interface SessionsUrlState {
  project: string;
  provider: string;
  deviceId: string;
  hideAutonomous: boolean;
  includeHidden: boolean;
  // `null` means no explicit range was chosen: a search (searchQuery is set)
  // then covers all indexed history, and a plain listing keeps the server's
  // default recent window. An explicit value always narrows either one.
  daysBack: number | null;
  searchQuery: string;
  aiSearch: boolean;
  sortOrder: SortOrder;
  limit: number;
}

export function parsePositiveIntParam(
  rawValue: string | null,
  fallback: number,
  min: number = 1,
  max: number = Number.POSITIVE_INFINITY,
): number {
  if (rawValue == null || rawValue.trim() === "") return fallback;
  const parsed = Number(rawValue);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.floor(parsed)));
}

export function readSessionsUrlState(
  searchParams: URLSearchParams,
): SessionsUrlState {
  const mode = searchParams.get("mode");
  const aiSearch =
    mode === "hybrid" ||
    mode === "semantic" ||
    mode === "smart" ||
    searchParams.get("semantic") === "1";
  const deviceId =
    searchParams.get("device_id") || searchParams.get("environment") || "";

  return {
    project: searchParams.get("project") || "",
    provider: searchParams.get("provider") || "",
    deviceId,
    hideAutonomous: searchParams.get("hide_autonomous") !== "false",
    includeHidden: searchParams.get("include_hidden") === "true",
    // Absent from the URL means "no explicit range" (null), not the old
    // recent-window default: the server now decides that default based on
    // whether a query is present.
    daysBack: searchParams.has("days_back")
      ? parsePositiveIntParam(searchParams.get("days_back"), DEFAULT_DAYS_BACK)
      : null,
    searchQuery: searchParams.get("query") || "",
    aiSearch,
    sortOrder:
      searchParams.get("sort") === "recent" ? "recent" : DEFAULT_SORT_ORDER,
    limit: parsePositiveIntParam(
      searchParams.get("limit"),
      PAGE_SIZE,
      PAGE_SIZE,
      100,
    ),
  };
}

export function buildSessionsSearchParams(
  state: SessionsUrlState,
): URLSearchParams {
  const params = new URLSearchParams();

  if (state.project) params.set("project", state.project);
  if (state.provider) params.set("provider", state.provider);
  if (state.deviceId) params.set("device_id", state.deviceId);
  if (state.daysBack !== null) params.set("days_back", String(state.daysBack));
  if (state.searchQuery) params.set("query", state.searchQuery);
  if (state.aiSearch) params.set("mode", "hybrid");
  if (state.searchQuery && state.sortOrder !== DEFAULT_SORT_ORDER)
    params.set("sort", state.sortOrder);
  if (!state.hideAutonomous) params.set("hide_autonomous", "false");
  if (state.includeHidden) params.set("include_hidden", "true");
  if (state.limit !== PAGE_SIZE) params.set("limit", String(state.limit));

  return params;
}
