import { describe, expect, it } from "vitest";
import { buildInboxLayout, isOnShelf, SHELF_RECENCY_MS } from "../timelineInbox";
import type {
  AgentSession,
  SessionCapabilities,
  SessionRuntimeDisplay,
  TimelineSessionCard,
} from "../../services/api/agents";
import { makeSessionStateFacts } from "../../test/sessionState";

function makeRuntimeDisplay(overrides: Partial<SessionRuntimeDisplay> = {}): SessionRuntimeDisplay {
  return {
    truth_tier: "none",
    signal_tier: "none",
    state: null,
    tone: "inactive",
    headline: "Inactive",
    detail: null,
    phase_label: "Inactive",
    compact_tool_label: null,
    is_live: false,
    is_executing: false,
    needs_attention: false,
    is_idle: true,
    is_stalled: false,
    is_managed_local_truth: false,
    has_signal: false,
    control_path: "unmanaged",
    activity_recency: "stale",
    lifecycle: "open",
    host_state: null,
    terminal_reason: null,
    ...overrides,
  };
}

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
    session_state: makeSessionStateFacts(),
    runtime_display: makeRuntimeDisplay(),
    timeline_card: null,
    capabilities: undefined,
    ...overrides,
  } as AgentSession;
}

function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    live_control_available: false,
    host_reattach_available: false,
    reply_to_live_session_available: false,
    observe_only: false,
    search_only: false,
    ...overrides,
  };
}

function makeCard(args: {
  id: string;
  repo: string;
  startedAt: string;
  closed?: boolean;
  endedAt?: string;
  lastActivityAt?: string;
  capabilities?: SessionCapabilities;
}): TimelineSessionCard {
  const session = makeSession({
    id: args.id,
    started_at: args.startedAt,
    ended_at: args.endedAt ?? null,
    last_activity_at: args.lastActivityAt ?? null,
    project: args.repo,
    capabilities: args.capabilities,
    session_state: makeSessionStateFacts({
      closed: args.closed,
      access: args.capabilities?.live_control_available
        ? "live_control"
        : args.capabilities?.host_reattach_available
          ? "reattach"
          : "search_only",
    }),
    runtime_display: makeRuntimeDisplay(args.closed ? { lifecycle: "closed" } : {}),
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

describe("isOnShelf", () => {
  const now = Date.parse("2026-05-18T12:00:00Z");

  it("returns false for closed sessions", () => {
    const card = makeCard({
      id: "c1",
      repo: "zerg",
      startedAt: "2026-05-18T11:00:00Z",
      closed: true,
      capabilities: makeCapabilities({ live_control_available: true }),
    });
    expect(isOnShelf(card, now)).toBe(false);
  });

  it("returns true for live-control sessions even if old", () => {
    const card = makeCard({
      id: "c1",
      repo: "zerg",
      startedAt: "2026-05-01T10:00:00Z",
      capabilities: makeCapabilities({ live_control_available: true }),
    });
    expect(isOnShelf(card, now)).toBe(true);
  });

  it("returns true for host-reattach sessions even if old", () => {
    const card = makeCard({
      id: "c1",
      repo: "zerg",
      startedAt: "2026-05-01T10:00:00Z",
      capabilities: makeCapabilities({ host_reattach_available: true }),
    });
    expect(isOnShelf(card, now)).toBe(true);
  });

  it("returns true for recent Shadow (<24h) without capabilities", () => {
    const recent = now - SHELF_RECENCY_MS + 60000; // 1 min inside window
    const card = makeCard({
      id: "c1",
      repo: "zerg",
      startedAt: new Date(recent).toISOString(),
    });
    expect(isOnShelf(card, now)).toBe(true);
  });

  it("returns false for old Shadow (>24h) without capabilities", () => {
    const old = now - SHELF_RECENCY_MS - 60000; // 1 min outside window
    const card = makeCard({
      id: "c1",
      repo: "zerg",
      startedAt: new Date(old).toISOString(),
    });
    expect(isOnShelf(card, now)).toBe(false);
  });

  it("returns false for sessions with capabilities unset and old", () => {
    const card = makeCard({
      id: "c1",
      repo: "zerg",
      startedAt: "2026-05-01T10:00:00Z",
    });
    expect(isOnShelf(card, now)).toBe(false);
  });
});

describe("buildInboxLayout", () => {
  // Use a fixed now far enough past all session dates that non-shelf
  // sessions (>24h old, no capabilities) stay in archive.
  const fixedNow = Date.parse("2026-05-20T12:00:00Z");

  it("groups sessions by repo and splits active from closed", () => {
    const cards = [
      makeCard({ id: "a1", repo: "floodmap", startedAt: "2026-05-18T12:00:00Z" }),
      makeCard({ id: "a2", repo: "floodmap", startedAt: "2026-05-18T11:00:00Z", closed: true }),
      makeCard({ id: "a3", repo: "zerg", startedAt: "2026-05-18T13:00:00Z" }),
    ];

    const layout = buildInboxLayout(cards, undefined, fixedNow);

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

    const layout = buildInboxLayout(cards, undefined, fixedNow);

    expect(layout.active[0].sessions.map((s) => s.thread_id)).toEqual([
      "new",
      "mid",
      "old",
    ]);
  });

  it("sorts closed sessions by close time descending, not start time", () => {
    const cards = [
      makeCard({
        id: "long-runner",
        repo: "zerg",
        startedAt: "2026-05-18T01:00:00Z",
        endedAt: "2026-05-18T02:00:00Z",
        closed: true,
      }),
      makeCard({
        id: "just-closed",
        repo: "zerg",
        startedAt: "2026-05-18T09:00:00Z",
        endedAt: "2026-05-18T12:00:00Z",
        closed: true,
      }),
      makeCard({
        id: "mid-closed",
        repo: "zerg",
        startedAt: "2026-05-18T08:00:00Z",
        endedAt: "2026-05-18T10:00:00Z",
        closed: true,
      }),
    ];

    const layout = buildInboxLayout(cards, undefined, fixedNow);

    expect(layout.closed[0].sessions.map((s) => s.thread_id)).toEqual([
      "just-closed",
      "mid-closed",
      "long-runner",
    ]);
  });

  it("orders closed repo groups by their most-recently-closed session", () => {
    const cards = [
      makeCard({
        id: "z",
        repo: "zerg",
        startedAt: "2026-05-18T01:00:00Z",
        endedAt: "2026-05-18T02:00:00Z",
        closed: true,
      }),
      makeCard({
        id: "f",
        repo: "floodmap",
        startedAt: "2026-05-18T00:00:00Z",
        endedAt: "2026-05-18T11:00:00Z",
        closed: true,
      }),
    ];

    expect(buildInboxLayout(cards, undefined, fixedNow).closed.map((g) => g.repo)).toEqual([
      "floodmap",
      "zerg",
    ]);
  });

  it("falls back to last_activity_at then start time when ended_at is null", () => {
    const cards = [
      makeCard({
        id: "start-only",
        repo: "zerg",
        startedAt: "2026-05-18T09:00:00Z",
        closed: true,
      }),
      makeCard({
        id: "activity-fallback",
        repo: "zerg",
        startedAt: "2026-05-18T07:00:00Z",
        lastActivityAt: "2026-05-18T11:00:00Z",
        closed: true,
      }),
      makeCard({
        id: "ended",
        repo: "zerg",
        startedAt: "2026-05-18T06:00:00Z",
        endedAt: "2026-05-18T13:00:00Z",
        closed: true,
      }),
    ];

    expect(buildInboxLayout(cards, undefined, fixedNow).closed[0].sessions.map((s) => s.thread_id)).toEqual([
      "ended",
      "activity-fallback",
      "start-only",
    ]);
  });

  it("orders repos by their newest active session", () => {
    const cards = [
      makeCard({ id: "f-old", repo: "floodmap", startedAt: "2026-05-17T10:00:00Z" }),
      makeCard({ id: "z-newest", repo: "zerg", startedAt: "2026-05-18T13:00:00Z" }),
      makeCard({ id: "s-mid", repo: "stopsign", startedAt: "2026-05-18T11:00:00Z" }),
    ];

    expect(buildInboxLayout(cards, undefined, fixedNow).active.map((g) => g.repo)).toEqual([
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

    const first = buildInboxLayout(cards, undefined, fixedNow);
    const second = buildInboxLayout(cards, undefined, fixedNow);

    expect(second.active[0].sessions.map((s) => s.thread_id)).toEqual(
      first.active[0].sessions.map((s) => s.thread_id),
    );
  });

  it("returns empty layout for empty input", () => {
    const layout = buildInboxLayout([]);
    expect(layout.shelf).toEqual([]);
    expect(layout.active).toEqual([]);
    expect(layout.closed).toEqual([]);
    expect(layout.closedCount).toBe(0);
  });

  it("applies a repo order override on top of default sort", () => {
    const cards = [
      makeCard({ id: "a", repo: "alpha", startedAt: "2026-05-18T10:00:00Z" }),
      makeCard({ id: "b", repo: "beta", startedAt: "2026-05-18T11:00:00Z" }),
      makeCard({ id: "c", repo: "gamma", startedAt: "2026-05-18T12:00:00Z" }),
    ];

    const layout = buildInboxLayout(cards, {
      shelfOrder: [],
      repoOrder: ["alpha"],
      sessionOrder: {},
    }, fixedNow);

    expect(layout.active.map((g) => g.repo)).toEqual(["alpha", "gamma", "beta"]);
  });

  it("applies a session order override within a repo", () => {
    const cards = [
      makeCard({ id: "first", repo: "zerg", startedAt: "2026-05-18T12:00:00Z" }),
      makeCard({ id: "second", repo: "zerg", startedAt: "2026-05-18T11:00:00Z" }),
      makeCard({ id: "third", repo: "zerg", startedAt: "2026-05-18T10:00:00Z" }),
    ];

    const layout = buildInboxLayout(cards, {
      shelfOrder: [],
      repoOrder: [],
      sessionOrder: { zerg: ["third"] },
    }, fixedNow);

    expect(layout.active[0].sessions.map((s) => s.thread_id)).toEqual([
      "third",
      "first",
      "second",
    ]);
  });

  it("ignores override entries for repos/sessions that no longer exist", () => {
    const cards = [
      makeCard({ id: "a", repo: "zerg", startedAt: "2026-05-18T12:00:00Z" }),
    ];

    const layout = buildInboxLayout(cards, {
      shelfOrder: [],
      repoOrder: ["ghost-repo", "zerg"],
      sessionOrder: { zerg: ["ghost-session", "a"] },
    }, fixedNow);

    expect(layout.active.map((g) => g.repo)).toEqual(["zerg"]);
    expect(layout.active[0].sessions.map((s) => s.thread_id)).toEqual(["a"]);
  });

  describe("shelf", () => {
    it("puts steerable (live control) sessions on shelf even if old", () => {
      const cards = [
        makeCard({
          id: "steerable",
          repo: "zerg",
          startedAt: "2026-05-01T10:00:00Z",
          capabilities: makeCapabilities({ live_control_available: true }),
        }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf.map((s) => s.thread_id)).toEqual(["steerable"]);
      expect(layout.active).toEqual([]);
    });

    it("puts host-reattach sessions on shelf even if old", () => {
      const cards = [
        makeCard({
          id: "reattachable",
          repo: "zerg",
          startedAt: "2026-05-01T10:00:00Z",
          capabilities: makeCapabilities({ host_reattach_available: true }),
        }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf.map((s) => s.thread_id)).toEqual(["reattachable"]);
      expect(layout.active).toEqual([]);
    });

    it("puts recent Shadow (<24h) on shelf", () => {
      const recentIso = new Date(fixedNow - 60 * 60 * 1000).toISOString(); // 1h ago
      const cards = [
        makeCard({ id: "recent", repo: "zerg", startedAt: recentIso }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf.map((s) => s.thread_id)).toEqual(["recent"]);
      expect(layout.active).toEqual([]);
    });

    it("puts old quiet Shadow in active archive, not shelf", () => {
      const cards = [
        makeCard({ id: "old-shadow", repo: "zerg", startedAt: "2026-05-01T10:00:00Z" }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf).toEqual([]);
      expect(layout.active[0].sessions.map((s) => s.thread_id)).toEqual(["old-shadow"]);
    });

    it("never puts closed sessions on shelf", () => {
      const cards = [
        makeCard({
          id: "closed-steerable",
          repo: "zerg",
          startedAt: "2026-05-18T11:59:00Z", // just before now
          closed: true,
          capabilities: makeCapabilities({ live_control_available: true }),
        }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf).toEqual([]);
      expect(layout.closed[0].sessions.map((s) => s.thread_id)).toEqual(["closed-steerable"]);
    });

    it("sorts shelf by start time desc by default", () => {
      const cards = [
        makeCard({
          id: "old",
          repo: "zerg",
          startedAt: "2026-05-01T10:00:00Z",
          capabilities: makeCapabilities({ live_control_available: true }),
        }),
        makeCard({
          id: "new",
          repo: "zerg",
          startedAt: "2026-05-18T10:00:00Z",
          capabilities: makeCapabilities({ live_control_available: true }),
        }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf.map((s) => s.thread_id)).toEqual(["new", "old"]);
    });

    it("applies shelf order override", () => {
      const cards = [
        makeCard({
          id: "alpha",
          repo: "zerg",
          startedAt: "2026-05-18T10:00:00Z",
          capabilities: makeCapabilities({ live_control_available: true }),
        }),
        makeCard({
          id: "beta",
          repo: "zerg",
          startedAt: "2026-05-18T11:00:00Z",
          capabilities: makeCapabilities({ live_control_available: true }),
        }),
      ];
      const layout = buildInboxLayout(cards, {
        shelfOrder: ["alpha"],
        repoOrder: [],
        sessionOrder: {},
      }, fixedNow);
      // beta is newer so default would be [beta, alpha], but override pins alpha first
      expect(layout.shelf.map((s) => s.thread_id)).toEqual(["alpha", "beta"]);
    });

    it("shelf plus archive plus closed coexist correctly", () => {
      const recentIso = new Date(fixedNow - 60 * 60 * 1000).toISOString();
      const cards = [
        makeCard({ id: "shelf-recent", repo: "zerg", startedAt: recentIso }),
        makeCard({
          id: "shelf-steerable",
          repo: "floodmap",
          startedAt: "2026-05-01T10:00:00Z",
          capabilities: makeCapabilities({ live_control_available: true }),
        }),
        makeCard({ id: "active-old", repo: "stopsign", startedAt: "2026-05-01T10:00:00Z" }),
        makeCard({ id: "closed", repo: "alpha", startedAt: "2026-05-01T10:00:00Z", closed: true }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf.map((s) => s.thread_id)).toEqual(["shelf-recent", "shelf-steerable"]);
      expect(layout.active.map((g) => g.repo)).toEqual(["stopsign"]);
      expect(layout.active[0].sessions.map((s) => s.thread_id)).toEqual(["active-old"]);
      expect(layout.closed.map((g) => g.repo)).toEqual(["alpha"]);
      expect(layout.shelfCount).toBe(2);
    });

    it("shelfCount is set even when zero", () => {
      const cards = [
        makeCard({ id: "old-shadow", repo: "zerg", startedAt: "2026-05-01T10:00:00Z" }),
      ];
      const layout = buildInboxLayout(cards, undefined, fixedNow);
      expect(layout.shelf).toEqual([]);
      expect(layout.shelfCount).toBe(0);
    });
  });
});
