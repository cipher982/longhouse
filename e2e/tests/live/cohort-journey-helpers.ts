import type { Page } from "@playwright/test";

export type JourneySession = {
  id: string;
  provider?: string | null;
  environment?: string | null;
  started_at: string;
  ended_at?: string | null;
  last_activity_at?: string | null;
  timeline_anchor_at?: string | null;
  user_messages?: number | null;
  assistant_messages?: number | null;
  tool_calls?: number | null;
};

export type JourneyCohorts = {
  active_recent: JourneySession | null;
  recent_closed: JourneySession | null;
  cold_gt_30d: JourneySession | null;
  older_projection: JourneySession | null;
  random_readable: JourneySession | null;
};

const DAY_MS = 24 * 60 * 60 * 1000;
const UUID_PATTERN = /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/i;
const SESSION_PATH_PATTERN = /\/(?:api\/)?(?:(?:timeline|agents)\/sessions|timeline)\/[A-Za-z0-9_-]+/i;
const QUERY_PARAMETER_PATTERN = /[?&](?:query|q)=/i;
const LOCAL_PATH_PATTERN = /(?:\/Users\/|\/home\/|[A-Za-z]:\\)/;
const FORBIDDEN_KEYS = new Set([
  "base_url",
  "cwd",
  "object_key",
  "path",
  "query",
  "session_id",
  "thread_id",
  "url",
]);

function timestampMs(session: JourneySession): number {
  for (const value of [
    session.timeline_anchor_at,
    session.last_activity_at,
    session.ended_at,
    session.started_at,
  ]) {
    const parsed = value ? Date.parse(value) : Number.NaN;
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function estimatedEntries(session: JourneySession): number {
  return Math.max(0, Number(session.user_messages ?? 0))
    + Math.max(0, Number(session.assistant_messages ?? 0))
    + Math.max(0, Number(session.tool_calls ?? 0));
}

function isEligible(session: JourneySession): boolean {
  if (!session.id || timestampMs(session) <= 0) return false;
  const environment = String(session.environment ?? "").toLowerCase();
  const provider = String(session.provider ?? "").toLowerCase();
  return !["test", "e2e", "automation"].includes(environment) && provider !== "canary";
}

function seededIndex(seed: string, size: number): number {
  let hash = 2166136261;
  for (const char of seed) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % size;
}

export function selectJourneyCohorts(
  input: JourneySession[],
  nowMs: number,
  randomSeed: string,
): JourneyCohorts {
  const byId = new Map<string, JourneySession>();
  for (const session of input) {
    if (isEligible(session) && !byId.has(session.id)) byId.set(session.id, session);
  }
  const sessions = [...byId.values()].sort((left, right) => timestampMs(right) - timestampMs(left));
  const ageMs = (session: JourneySession) => nowMs - timestampMs(session);

  const activeRecent = sessions.find(
    (session) => !session.ended_at && ageMs(session) >= 0 && ageMs(session) <= 30 * DAY_MS,
  ) ?? sessions.find((session) => ageMs(session) >= 0 && ageMs(session) <= 30 * DAY_MS) ?? null;
  const recentClosed = sessions.find(
    (session) => Boolean(session.ended_at) && ageMs(session) >= 0 && ageMs(session) <= 30 * DAY_MS,
  ) ?? null;
  const cold = sessions.find(
    (session) => ageMs(session) > 30 * DAY_MS && ageMs(session) <= 90 * DAY_MS,
  ) ?? null;
  const pagination = [...sessions]
    .filter((session) => estimatedEntries(session) > 200)
    .sort((left, right) => estimatedEntries(right) - estimatedEntries(left))[0] ?? null;

  const used = new Set([activeRecent?.id, recentClosed?.id, cold?.id, pagination?.id].filter(Boolean));
  const randomPool = sessions.filter((session) => !used.has(session.id));
  const random = randomPool.length > 0 ? randomPool[seededIndex(randomSeed, randomPool.length)] : null;

  return {
    active_recent: activeRecent,
    recent_closed: recentClosed,
    cold_gt_30d: cold,
    older_projection: pagination,
    random_readable: random,
  };
}

export function resultCountBucket(count: number): "0" | "1" | "2-5" | "6-20" | "21+" {
  if (count <= 0) return "0";
  if (count === 1) return "1";
  if (count <= 5) return "2-5";
  if (count <= 20) return "6-20";
  return "21+";
}

export function classifyApiResource(rawUrl: string): string | null {
  let url: URL;
  try {
    url = new URL(rawUrl, "https://journey.invalid");
  } catch {
    return null;
  }
  const path = url.pathname.replace(/^\/api/, "");
  if (path === "/health") return "health";
  if (path === "/timeline/recall") return "recall";
  if (path === "/timeline/sessions") return url.searchParams.has("query") ? "lexical_search" : "timeline_list";
  if (/^\/timeline\/sessions\/[^/]+\/workspace$/.test(path)) return "session_workspace";
  if (/^\/timeline\/sessions\/[^/]+\/projection$/.test(path)) return "session_projection";
  if (/^\/timeline\/sessions\/[^/]+\/thread$/.test(path)) return "session_thread";
  if (/^\/timeline\/sessions\/[^/]+\/turns$/.test(path)) return "session_turns";
  if (/^\/timeline\/sessions\/[^/]+$/.test(path)) return "session_detail";
  return null;
}

export function classifyJourneyFailure(error: unknown): string {
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (message.includes("timeout")) return "timeout";
  if (message.includes("http_4")) return "http_4xx";
  if (message.includes("http_5")) return "http_5xx";
  if (message.includes("empty_result")) return "empty_result";
  if (message.includes("missing_cohort")) return "missing_cohort";
  if (message.includes("fixture_not_configured")) return "fixture_not_configured";
  if (message.includes("demo_target")) return "demo_target";
  if (message.includes("build_identity")) return "build_identity_unavailable";
  if (message.includes("projection_not_appended")) return "projection_not_appended";
  if (message.includes("paint_evidence_unavailable")) return "paint_evidence_unavailable";
  return "browser_or_contract_failure";
}

function visit(value: unknown, forbiddenStrings: string[]): void {
  if (Array.isArray(value)) {
    for (const item of value) visit(item, forbiddenStrings);
    return;
  }
  if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      if (FORBIDDEN_KEYS.has(key.toLowerCase())) throw new Error(`privacy_forbidden_key:${key}`);
      visit(item, forbiddenStrings);
    }
    return;
  }
  if (typeof value !== "string") return;
  if (UUID_PATTERN.test(value)) throw new Error("privacy_uuid");
  if (SESSION_PATH_PATTERN.test(value)) throw new Error("privacy_session_path");
  if (QUERY_PARAMETER_PATTERN.test(value)) throw new Error("privacy_query_parameter");
  if (LOCAL_PATH_PATTERN.test(value)) throw new Error("privacy_local_path");
  for (const forbidden of forbiddenStrings.filter(Boolean)) {
    if (value === forbidden) throw new Error("privacy_fixture_value");
  }
}

export function assertPrivacySafeArtifact(payload: unknown, forbiddenStrings: string[] = []): void {
  visit(payload, forbiddenStrings);
}

export async function waitForElementPaint(page: Page, marker: string, afterEpochMs: number): Promise<number> {
  const epochMs = await page.evaluate(
    ({ expectedMarker, minimumEpochMs, timeoutMs }) => new Promise<number | false>((resolve) => {
      let observer: PerformanceObserver | null = null;
      let timeoutId = 0;
      const finish = (value: number | false) => {
        window.clearTimeout(timeoutId);
        observer?.disconnect();
        resolve(value);
      };
      const inspect = (entries: PerformanceEntry[]) => {
        const match = (entries as Array<PerformanceEntry & { identifier?: string }>).find((entry) => (
          entry.identifier === expectedMarker
          && performance.timeOrigin + entry.startTime >= minimumEpochMs - 25
        ));
        if (match) finish(performance.timeOrigin + match.startTime);
      };

      timeoutId = window.setTimeout(() => finish(false), timeoutMs);
      try {
        observer = new PerformanceObserver((list) => inspect(list.getEntries()));
        observer.observe({ type: "element", buffered: true });
      } catch {
        finish(false);
      }
    }),
    { expectedMarker: marker, minimumEpochMs: afterEpochMs, timeoutMs: 15_000 },
  );
  if (typeof epochMs !== "number" || !Number.isFinite(epochMs)) throw new Error("paint_evidence_unavailable");
  return epochMs;
}
