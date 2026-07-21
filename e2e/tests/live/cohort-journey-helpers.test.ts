import { describe, expect, test } from "bun:test";
import {
  assertPrivacySafeArtifact,
  classifyApiResource,
  classifyJourneyFailure,
  resultCountBucket,
  selectJourneyCohorts,
  type JourneySession,
} from "./cohort-journey-helpers";

const NOW = Date.parse("2026-07-20T12:00:00Z");
const day = (daysAgo: number) => new Date(NOW - daysAgo * 24 * 60 * 60 * 1000).toISOString();
const session = (id: string, daysAgo: number, extra: Partial<JourneySession> = {}): JourneySession => ({
  id,
  provider: "codex",
  environment: "production",
  started_at: day(daysAgo),
  last_activity_at: day(daysAgo),
  ...extra,
});

describe("cohort journey helpers", () => {
  test("selects recent, closed, cold, paginated, and deterministic random cohorts", () => {
    const cohorts = selectJourneyCohorts([
      session("recent", 1),
      session("closed", 2, { ended_at: day(2) }),
      session("cold", 45),
      session("large", 10, { user_messages: 120, assistant_messages: 120, tool_calls: 30 }),
      session("random-a", 12),
      session("random-b", 15),
      session("ignored-test", 3, { environment: "test" }),
    ], NOW, "2026-07-20");

    expect(cohorts.active_recent?.id).toBe("recent");
    expect(cohorts.recent_closed?.id).toBe("closed");
    expect(cohorts.cold_gt_30d?.id).toBe("cold");
    expect(cohorts.older_projection?.id).toBe("large");
    expect(["random-a", "random-b"]).toContain(cohorts.random_readable?.id);
    expect(selectJourneyCohorts([
      session("random-a", 12),
      session("random-b", 15),
    ], NOW, "same-seed").random_readable?.id).toBe(
      selectJourneyCohorts([
        session("random-a", 12),
        session("random-b", 15),
      ], NOW, "same-seed").random_readable?.id,
    );
  });

  test("does not misclassify sessions outside the controlled 90-day cold window", () => {
    const cohorts = selectJourneyCohorts([session("too-old", 91)], NOW, "seed");
    expect(cohorts.cold_gt_30d).toBeNull();
  });

  test("classifies API resources without retaining identifiers or queries", () => {
    expect(classifyApiResource("https://x/api/timeline/sessions?query=private")).toBe("lexical_search");
    expect(classifyApiResource("https://x/api/timeline/sessions/abc-123/projection?cursor=secret")).toBe("session_projection");
    expect(classifyApiResource("https://x/api/timeline/recall?query=private")).toBe("recall");
  });

  test("buckets counts and emits bounded failure classes", () => {
    expect([0, 1, 4, 12, 22].map(resultCountBucket)).toEqual(["0", "1", "2-5", "6-20", "21+"]);
    expect(classifyJourneyFailure(new Error("fixture_not_configured"))).toBe("fixture_not_configured");
    expect(classifyJourneyFailure(new Error("secret transcript text"))).toBe("browser_or_contract_failure");
  });

  test("rejects high-cardinality or content-bearing artifact fields", () => {
    const safe = {
      schema_version: 1,
      traffic_class: "synthetic",
      phases: [{ phase: "cold_session", outcome: "pass", route_class: "session_detail" }],
    };
    expect(() => assertPrivacySafeArtifact(safe, ["private fixture"])).not.toThrow();
    expect(() => assertPrivacySafeArtifact({ session_id: "hidden" })).toThrow();
    expect(() => assertPrivacySafeArtifact({ detail: "/timeline/sessions/abc123" })).toThrow();
    expect(() => assertPrivacySafeArtifact({ detail: "?query=hidden" })).toThrow();
    expect(() => assertPrivacySafeArtifact({ detail: "/Users/david/private" })).toThrow();
    expect(() => assertPrivacySafeArtifact({ detail: "private fixture" }, ["private fixture"])).toThrow();
  });
});
