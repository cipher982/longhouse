import { describe, expect, it } from "vitest";
import {
  H_FLOOR,
  H_TOP,
  SessionHeat,
  TAU_BED,
  TAU_CORE,
  T_AMB,
  T_LIT,
  T_WAITING,
  cool,
  densityWork,
  diffSnapshots,
  hearthModeForLamp,
  hearthSnapshotFromSession,
  initialBed,
  kindOf,
  targetHeight,
  type HearthEvent,
  type HearthSnapshot,
} from "../hearth/signals";
import { makeSessionStateFacts } from "@/shared/test/sessionState";

const NOW = Date.parse("2026-09-27T16:00:00Z");

function snap(overrides: Partial<HearthSnapshot> = {}): HearthSnapshot {
  return {
    mode: "working",
    toolCalls: 10,
    assistantMessages: 10,
    userMessages: 2,
    subagents: 0,
    tool: null,
    lastActivityMs: NOW,
    startedMs: NOW - 3_600_000,
    ...overrides,
  };
}

function drain(heat: SessionHeat, from: number, to: number): HearthEvent[] {
  const events: HearthEvent[] = [];
  for (let t = from; t <= to + 1e-9; t += 0.05) heat.step(t, 0.05, NOW, (e) => events.push(e));
  return events;
}

describe("card deltas", () => {
  it("turns count increases into tool, reply and prompt events", () => {
    expect(diffSnapshots(snap(), snap({ toolCalls: 13, assistantMessages: 11, userMessages: 3 }))).toEqual({ tools: 3, messages: 1, prompts: 1 });
  });

  it("treats the first sighting and a count reset as a baseline, not events", () => {
    expect(diffSnapshots(null, snap())).toEqual({ tools: 0, messages: 0, prompts: 0 });
    expect(diffSnapshots(snap({ toolCalls: 90 }), snap({ toolCalls: 4 }))).toEqual({ tools: 0, messages: 0, prompts: 0 });
  });

  it("plays a batch out over time, prompt first, with the tool kind the card named", () => {
    const heat = new SessionHeat();
    heat.update(snap(), 0, NOW);
    heat.update(snap({ toolCalls: 12, userMessages: 3, tool: "Bash" }), 2, NOW);
    const early = drain(heat, 2, 2.01);
    expect(early.map((e) => e.type)).toEqual(["prompt"]);
    const rest = drain(heat, 2.05, 5);
    expect(rest).toHaveLength(2);
    expect(rest.every((e) => e.type === "tool" && e.kind === "exec")).toBe(true);
  });

  it("keeps the last named tool for a batch that lands while the agent thinks", () => {
    const heat = new SessionHeat();
    heat.update(snap({ tool: "Edit" }), 0, NOW);
    heat.update(snap({ toolCalls: 11, tool: null }), 1, NOW);
    const events = drain(heat, 1, 4);
    expect(events).toEqual([{ type: "tool", kind: "edit", name: "Edit" }]);
  });

  it("credits at most a bounded batch when a reconnect reports a huge jump", () => {
    const heat = new SessionHeat();
    heat.update(snap(), 0, NOW);
    heat.update(snap({ toolCalls: 10 + 5_000_000, assistantMessages: 10 + 5_000_000, userMessages: 2 + 3 }), 1, NOW);
    const events = drain(heat, 1, 5);
    expect(events.length).toBeLessThanOrEqual(8);
    expect(events[0]).toEqual({ type: "prompt" });
    // Unshown events still count as work, but only up to the cap.
    expect(heat.work).toBeLessThan(24 * 150);
  });

  it("caps how many events of one burst become sparks", () => {
    const heat = new SessionHeat();
    heat.update(snap(), 0, NOW);
    heat.update(snap({ toolCalls: 400 }), 10, NOW);
    expect(drain(heat, 10, 14).length).toBeLessThanOrEqual(8);
  });
});

describe("work curve", () => {
  it("gives any active session a pilot flame and saturates below the top", () => {
    expect(targetHeight(0, false)).toBe(0);
    expect(targetHeight(0, true)).toBeCloseTo(H_FLOOR);
    expect(targetHeight(60, true)).toBeGreaterThan(0.65);
    expect(targetHeight(10_000, true)).toBeLessThanOrEqual(H_TOP);
  });

  it("reads idle, working and very busy apart", () => {
    const working = new SessionHeat();
    working.update(snap({ toolCalls: 20 }), 0, NOW);
    const busy = new SessionHeat();
    busy.update(snap({ toolCalls: 20 }), 0, NOW);
    for (let i = 1; i <= 30; i++) {
      busy.update(snap({ toolCalls: 20 + i * 3, assistantMessages: 10 + i }), i, NOW);
      drain(busy, i - 0.95, i);
      drain(working, i - 0.95, i);
    }
    const idle = new SessionHeat();
    idle.update(snap({ mode: "idle", lastActivityMs: NOW - 60_000 }), 0, NOW);
    expect(idle.target(30)).toBe(0);
    expect(working.target(30)).toBeGreaterThanOrEqual(H_FLOOR);
    expect(busy.target(30) - working.target(30)).toBeGreaterThan(0.15);
  });

  it("starts a session with a dense tool history as a bigger fire", () => {
    const dense = snap({ toolCalls: 1800, startedMs: NOW - 3_600_000 });
    expect(densityWork(dense)).toBeGreaterThan(30);
    expect(densityWork(snap({ toolCalls: 20 }))).toBeLessThan(2);
    const heat = new SessionHeat();
    heat.update(dense, 0, NOW);
    expect(heat.target(0)).toBeGreaterThan(targetHeight(20, true));
  });

  it("stops fuel once a session goes quiet", () => {
    const heat = new SessionHeat();
    heat.update(snap({ mode: "working" }), 0, NOW);
    heat.update(snap({ mode: "idle", toolCalls: 11 }), 1, NOW);
    drain(heat, 1, 4);
    expect(heat.target(4)).toBeGreaterThan(0);
    drain(heat, 4.05, 12);
    expect(heat.target(12)).toBe(0);
  });
});

describe("cooling", () => {
  it("cools the surface over ~20 min and the deep embers over ~3 h", () => {
    expect(cool(T_LIT, 0, TAU_BED)).toBe(T_LIT);
    expect(cool(T_LIT, TAU_BED, TAU_BED)).toBeCloseTo(T_AMB + (T_LIT - T_AMB) / Math.E);
    const recent = initialBed(snap({ mode: "idle", lastActivityMs: NOW - 2 * 60_000 }), NOW);
    const hours = initialBed(snap({ mode: "idle", lastActivityMs: NOW - 4 * 3_600_000 }), NOW);
    expect(recent.surface).toBeGreaterThan(1000); // orange
    expect(hours.surface).toBeLessThan(320); // black
    expect(hours.core).toBeCloseTo(cool(T_LIT, 4 * 3600, TAU_CORE)); // a faint red in the cracks
    expect(hours.core).toBeGreaterThan(hours.surface);
  });

  it("cools an idle bed exactly once on first sight", () => {
    const idle = snap({ mode: "idle", lastActivityMs: NOW - 20 * 60_000 });
    const expected = initialBed(idle, NOW);
    const heat = new SessionHeat();
    heat.update(idle, 0, NOW);
    heat.step(0.016, 0.016, NOW);
    expect(heat.surface).toBeCloseTo(expected.surface, 3);
    expect(heat.core).toBeCloseTo(expected.core, 3);
    expect(heat.surface).toBeCloseTo(cool(T_LIT, 20 * 60, TAU_BED), 3);
  });

  it("keeps a waiting session's coals warm and an ended one's cold", () => {
    const waiting = initialBed(snap({ mode: "waiting", lastActivityMs: NOW - 3 * 3_600_000 }), NOW);
    expect(waiting.surface).toBeGreaterThanOrEqual(T_WAITING);
    expect(initialBed(snap({ mode: "ended" }), NOW)).toEqual({ surface: T_AMB, core: T_AMB });
  });

  it("cools from the moment work stops, in wall time", () => {
    const heat = new SessionHeat();
    heat.update(snap({ mode: "working" }), 0, NOW);
    drain(heat, 0, 30);
    const stopped = snap({ mode: "idle", lastActivityMs: NOW });
    heat.update(stopped, 30, NOW);
    heat.step(40, 0.1, NOW);
    const hot = heat.coolBed(NOW).surface;
    const later = heat.coolBed(NOW + 20 * 60_000).surface;
    expect(hot).toBeGreaterThan(1100);
    expect(later - T_AMB).toBeCloseTo((hot - T_AMB) / Math.E, 0);
  });
});

describe("mapping from the timeline row", () => {
  it("classifies tool names into spark kinds", () => {
    expect(kindOf("Bash")).toBe("exec");
    expect(kindOf("apply_patch")).toBe("edit");
    expect(kindOf("Grep")).toBe("read");
    expect(kindOf("Task")).toBe("agent");
    expect(kindOf("mcp__hatch__hatch_codex")).toBe("agent");
    expect(kindOf("TodoWrite")).toBe("other");
    expect(kindOf(null)).toBe("other");
  });

  it("maps row lamp states to fire modes", () => {
    expect(hearthModeForLamp("working")).toBe("working");
    expect(hearthModeForLamp("waiting")).toBe("waiting");
    expect(hearthModeForLamp("ended")).toBe("ended");
    expect(hearthModeForLamp("done")).toBe("idle");
    expect(hearthModeForLamp("unknown")).toBe("idle");
  });

  it("reads parent counts and fresh subagent roots without counting command or monitor kinds", () => {
    const facts = makeSessionStateFacts({});
    const session = {
      id: "parent",
      tool_calls: 42,
      assistant_messages: 7,
      user_messages: 3,
      last_activity_at: "2026-09-27T15:59:00Z",
      started_at: "2026-09-27T15:00:00Z",
      session_state: {
        ...facts,
        activity: { ...facts.activity, state: "executing" as const, tool: "Bash" },
        delegation: {
          state: "pending" as const,
          count: 4,
          kinds: { subagent: 2, shell: 1, monitor: 1 },
          valid_until: "2026-09-27T16:05:00Z",
          items: [
            {
              id: "child",
              kind: "subagent",
              status: "running",
              session_id: "child",
              tool_calls: 11,
              assistant_messages: 4,
              user_messages: 1,
            },
            { id: "unlinked", kind: "subagent", status: "running", session_id: null },
            { id: "shell", kind: "shell", status: "running", session_id: null },
            {
              id: "monitor",
              kind: "monitor",
              status: "running",
              session_id: null,
              tool_calls: null,
            },
          ],
        },
      },
    };
    const active = hearthSnapshotFromSession(session, "working", NOW);
    expect(active.subagents).toBe(2);
    expect(Object.keys(active.children ?? {})).toEqual(["child"]);
    expect(hearthSnapshotFromSession(session, "working", NOW + 6 * 60_000).subagents).toBe(0);
  });

  it("preserves continuing children while other children join, leave, or reconnect", () => {
    const first = snap({ children: {
      alpha: { toolCalls: 11, assistantMessages: 4, userMessages: 1 },
      beta: { toolCalls: 20, assistantMessages: 5, userMessages: 1 },
    } });
    const joined = snap({ children: {
      alpha: { toolCalls: 15, assistantMessages: 6, userMessages: 2 },
      gamma: { toolCalls: 500, assistantMessages: 100, userMessages: 20 },
    } });
    expect(diffSnapshots(first, joined)).toEqual({ tools: 4, messages: 2, prompts: 1 });
    const reconnected = snap({ children: {
      alpha: { toolCalls: 16, assistantMessages: 7, userMessages: 2 },
      beta: { toolCalls: 25, assistantMessages: 7, userMessages: 3 },
    } });
    expect(diffSnapshots(joined, reconnected)).toEqual({ tools: 1, messages: 1, prompts: 0 });
    const next = snap({ children: {
      alpha: { toolCalls: 17, assistantMessages: 8, userMessages: 2 },
      beta: { toolCalls: 27, assistantMessages: 7, userMessages: 4 },
    } });
    expect(diffSnapshots(reconnected, next)).toEqual({ tools: 3, messages: 1, prompts: 1 });
  });

  it("baselines each newly known child counter without losing other known work", () => {
    const unknown = snap({ children: { alpha: { toolCalls: null, assistantMessages: 4, userMessages: null } } });
    const known = snap({ children: { alpha: { toolCalls: 500, assistantMessages: 5, userMessages: 20 } } });
    expect(diffSnapshots(unknown, known)).toEqual({ tools: 0, messages: 1, prompts: 0 });
    const next = snap({ children: { alpha: { toolCalls: 502, assistantMessages: 6, userMessages: 21 } } });
    expect(diffSnapshots(known, next)).toEqual({ tools: 2, messages: 1, prompts: 1 });
  });
});
