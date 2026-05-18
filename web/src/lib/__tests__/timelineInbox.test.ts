import { describe, expect, it } from "vitest";
import { buildInboxLayout } from "../timelineInbox";
import type { AgentSession, TimelineSessionCard } from "../../services/api/agents";

function makeSession(overrides: Partial<AgentSession> & { id: string }): AgentSession {
  return {
    id: overrides.id,
    provider: "claude",
    started_at: "2026-05-18T10:00:00Z",
    ended_at: null,
    last_activity_at: null,
    timeline_anchor_at: null,
    project: null,
    cwd: null,
    git_repo: null,
    git_branch: null,
    summary_title: null,
    summary: null,
    first_user_message: null,
    user_messages: 0,
    tool_calls: 0,
    terminal_state: null,
    runtime_facts: null,
    runtime_display: null,
    timeline_card: null,
    capabilities: undefined,
    ...overrides,
  } as AgentSession;
}

function makeCard(args: {
  id: string;
  repo: string;
  startedAt: string;
  closed?: boolean;
}): TimelineSessionCard {
  const session = makeSession({
    id: args.id,
    started_at: args.startedAt,
    project: args.repo,
    runtime_facts: args.closed
      ? ({ lifecycle: { state: "closed" } } as AgentSession["runtime_facts"])
      : null,
  });
  return {
    thread_id: args.id,
    timeline_anchor_at: args.startedAt,
    head: session,
    detail: session,
    root: session,
    continuation_count: 1,
    started_origin_label: null,
    head_origin_label: null,
  };
}

describe("buildInboxLayout", () => {
  it("groups sessions by repo and splits active from closed", () => {
    const cards = [
      makeCard({ id: "a1", repo: "floodmap", startedAt: "2026-05-18T12:00:00Z" }),
      makeCard({ id: "a2", repo: "floodmap", startedAt: "2026-05-18T11:00:00Z", closed: true }),
      makeCard({ id: "a3", repo: "zerg", startedAt: "2026-05-18T13:00:00Z" }),
    ];

    const layout = buildInboxLayout(cards);

    expect(layout.active.map((g) => g.repo)).toEqual(["zerg", "floodmap"]);
    expect(layout.active[0].sessions.map((s) => s.thread_id)).toEqual(["a3"]);
    expect(layout.active[1].sessions.map((s) => s.thread_id)).toEqual(["a1"]);

    expect(layout.closed.map((g) => g.repo)).toEqual(["floodmap"]);
    expect(layout.closed[0].sessions.map((s) => s.thread_id)).toEqual(["a2"]);
    expect(layout.closedCount).toBe(1);
  });

  it("sorts sessions within a repo by start time descending (frozen)", () => {
    const cards = [
      makeCard({ id: "old", repo: "zerg", startedAt: "2026-05-17T10:00:00Z" }),
      makeCard({ id: "new", repo: "zerg", startedAt: "2026-05-18T10:00:00Z" }),
      makeCard({ id: "mid", repo: "zerg", startedAt: "2026-05-18T05:00:00Z" }),
    ];

    const layout = buildInboxLayout(cards);

    expect(layout.active[0].sessions.map((s) => s.thread_id)).toEqual([
      "new",
      "mid",
      "old",
    ]);
  });

  it("orders repos by their newest active session", () => {
    const cards = [
      makeCard({ id: "f-old", repo: "floodmap", startedAt: "2026-05-17T10:00:00Z" }),
      makeCard({ id: "z-newest", repo: "zerg", startedAt: "2026-05-18T13:00:00Z" }),
      makeCard({ id: "s-mid", repo: "stopsign", startedAt: "2026-05-18T11:00:00Z" }),
    ];

    expect(buildInboxLayout(cards).active.map((g) => g.repo)).toEqual([
      "zerg",
      "stopsign",
      "floodmap",
    ]);
  });

  it("re-running on the same input is stable (jitter regression)", () => {
    const cards = [
      makeCard({ id: "a", repo: "zerg", startedAt: "2026-05-18T12:00:00Z" }),
      makeCard({ id: "b", repo: "zerg", startedAt: "2026-05-18T11:00:00Z" }),
    ];

    const first = buildInboxLayout(cards);
    const second = buildInboxLayout(cards);

    expect(second.active[0].sessions.map((s) => s.thread_id)).toEqual(
      first.active[0].sessions.map((s) => s.thread_id),
    );
  });

  it("returns empty layout for empty input", () => {
    const layout = buildInboxLayout([]);
    expect(layout.active).toEqual([]);
    expect(layout.closed).toEqual([]);
    expect(layout.closedCount).toBe(0);
  });
});
