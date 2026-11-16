import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentEvent, AgentSessionProjectionItem } from "@/shared/api/agents";
import { presentTranscript } from "../presentTranscript";
import type { SubagentChild } from "../types";

function fixture(name: string): { projection: { items: AgentSessionProjectionItem[] } } {
  return JSON.parse(readFileSync(resolve(process.cwd(), "../tests/fixtures/transcript-payload", name), "utf8"));
}

function ev(partial: Partial<AgentEvent> & { id: number; role: string }): AgentSessionProjectionItem {
  const event = {
    content_text: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp: `2026-11-15T10:00:${String(partial.id).padStart(2, "0")}Z`,
    in_active_context: true,
    ...partial,
  } as AgentEvent;
  return { kind: "event", event, timestamp: event.timestamp } as unknown as AgentSessionProjectionItem;
}

const asJson = (value: unknown) => JSON.parse(JSON.stringify(value));

describe("presentTranscript hostile transcript", () => {
  it("equals the checked-in golden rows", () => {
    const rows = presentTranscript({ items: fixture("hostile-transcript.json").projection.items });
    const golden = JSON.parse(readFileSync(resolve(process.cwd(), "../tests/fixtures/transcript-payload/hostile-transcript.golden.json"), "utf8"));
    expect(asJson(rows)).toEqual(golden);
  });

  it("keeps the key semantics", () => {
    const rows = presentTranscript({ items: fixture("hostile-transcript.json").projection.items });
    expect(rows[0].kind).toBe("message");
    const activity = rows.find((row) => row.kind === "activityGroup");
    expect(activity?.calls?.length).toBe(4);
    expect(rows.some((row) => row.status === "orphan")).toBe(true);
  });
});

describe("presentTranscript Claude fidelity (port of the Swift payload test)", () => {
  const subagents = [
    {
      session_id: "sub-1", provider: "claude", parent_tool_call_id: "toolu_agent_1", run_id: null,
      started_at: "2026-11-15T10:00:00Z", ended_at: null, user_messages: 1, assistant_messages: 2, tool_calls: 5,
      title: "Parent task", first_user_message_preview: null, last_visible_text_preview: null,
      agent_type: "general-purpose", description: "Audit runtime dock", is_fork: true, spawn_depth: 1,
      live_status: "running", duration_ms: 120_000, total_tokens: 54_000,
    },
    {
      session_id: "sub-2", provider: "claude", parent_tool_call_id: null, run_id: null,
      started_at: "2026-11-15T10:00:30Z", ended_at: null, user_messages: 1, assistant_messages: 1, tool_calls: 2,
      title: "Child task", first_user_message_preview: null, last_visible_text_preview: null,
      parent_agent_id: "sub-1", agent_type: "Explore", description: "Inspect Swift files", is_fork: false,
      spawn_depth: 2, live_status: "waiting", duration_ms: 45_000, total_tokens: 12_500,
    },
  ] as SubagentChild[];

  const items = [
    ev({ id: 1, role: "system", interaction_kind: "provider_reasoning", reasoning_withheld: true }),
    ev({ id: 2, role: "assistant", content_text: "Checking child subagent status.", message_phase: "commentary" }),
    ev({ id: 3, role: "assistant", tool_name: "Agent", tool_call_id: "toolu_agent_1", tool_call_state: "running",
      tool_input_json: { description: "Audit runtime dock", subagent_type: "general-purpose" },
      tool_output_text: "Working on sub-1 output", tool_intent: "Audit Claude subagent parity" }),
    ev({ id: 4, role: "system", content_text: "Agent finished", interaction_kind: "provider_notification",
      task_notification: { status: "completed", duration_ms: 5000, subagent_tokens: 12000 } }),
    ev({ id: 5, role: "system", content_text: "Waiting for 2 background agents to finish", interaction_kind: "provider_notification", waiting_background_agents: 2 }),
    ev({ id: 6, role: "system", content_text: "Overloaded", interaction_kind: "provider_notification",
      api_error: { message: "Overloaded", retry_attempt: 1, max_retries: 3 } }),
    ev({ id: 7, role: "assistant", content_text: "Done.", message_phase: "final_answer",
      turn_end: { duration_ms: 12000, ended_at: "2026-11-15T10:02:00Z", outcome: "completed", stop_hook_summary: "All clean" } }),
  ];

  it("carries the new signals and nested subagents", () => {
    const rows = presentTranscript({ items, subagents, now: new Date("2026-11-15T12:00:00Z"), locale: "en-US", timeZone: "UTC" });
    expect(rows.length).toBe(7);
    expect(rows[0].reasoningWithheld).toBe(true);
    expect(rows[1].phase).toBe("commentary");
    expect(rows[2].toolIntent).toBe("Audit Claude subagent parity");
    expect(rows[2].startedAtMs).toBeTypeOf("number");
    expect(rows[2].output).toBe("Working on sub-1 output");
    expect(rows[2].subagents?.map((s) => s.sessionId)).toEqual(["sub-1", "sub-2"]);
    expect(rows[2].subagents?.map((s) => s.depth ?? 0)).toEqual([0, 1]);
    expect(rows[2].subagents?.[0].isFork).toBe(true);
    expect(rows[3].taskNotification).toBeTruthy();
    expect(rows[4].waitingBackgroundAgents).toBe(2);
    expect(rows[5].apiError).toBeTruthy();
    expect(rows[6].turnEnd).toEqual({ label: "Worked for 12s", doneAt: expect.stringMatching(/^Turn finished 10:02.AM · All clean$/) });
  });

  it("flags the tool row that trails commentary (narration)", () => {
    const rows = presentTranscript({ items, subagents });
    expect(rows[1].narrationTrail).toBeUndefined();
    expect(rows[2].narrationTrail).toBe(true);
    expect(rows[6].narrationTrail).toBeUndefined();
  });
});

describe("presentTranscript policies", () => {
  it("collapses consecutive wake notices into one counted row", () => {
    const wake = (id: number) => ev({ id, role: "user", content_text: `job ${id} done`, input_origin: { authored_via: "terminal", origin: "wake" } });
    const rows = presentTranscript({ items: [wake(1), wake(2), wake(3), ev({ id: 4, role: "assistant", content_text: "ok" }), wake(5)] });
    expect(rows.map((r) => r.id)).toEqual(["user:3", "prose:4", "user:5"]);
    expect(rows[0].body).toBe("job 3 done (3 updates)");
    expect(rows[0].wake).toBe(true);
  });

  it("truncates long output and collapses long messages", () => {
    const long = Array.from({ length: 700 }, (_, i) => `line ${i}`).join("\n");
    const rows = presentTranscript({
      items: [
        ev({ id: 1, role: "assistant", content_text: long }),
        ev({ id: 2, role: "assistant", tool_name: "Write", tool_call_id: "c" }),
        ev({ id: 3, role: "tool", tool_call_id: "c", tool_output_text: "x".repeat(13_000) }),
      ],
    });
    expect(rows[0].collapsed).toBe(true);
    expect(rows[0].fullBody).toBe(long);
    expect(rows[0].body).toContain("... 400 lines hidden ...");
    expect(rows[1].output?.endsWith("\n... truncated in iOS transcript ...")).toBe(true);
    expect(rows[1].output?.length).toBe(12_000 + "\n... truncated in iOS transcript ...".length);
  });

  it("uses the Failed and Interrupted turn-end verbs", () => {
    const end = (outcome: string) => presentTranscript({
      items: [ev({ id: 1, role: "assistant", content_text: "x", turn_end: { duration_ms: 65_000, ended_at: "2026-11-15T10:02:00Z", outcome } })],
      now: new Date("2026-11-15T12:00:00Z"), locale: "en-US", timeZone: "UTC",
    })[0].turnEnd;
    expect(end("error")?.label).toBe("Failed after 1m 5s");
    expect(end("interrupted")?.label).toBe("Interrupted after 1m 5s");
    expect(end("interrupted")?.doneAt).toMatch(/^stopped 10:02/);
  });
});

describe("presentTranscript lite pages", () => {
  const lite = JSON.parse(
    readFileSync(resolve(process.cwd(), "../tests/fixtures/session-detail/lite-mobile-tail.json"), "utf8"),
  ) as { projection: { items: AgentSessionProjectionItem[] } };
  const rowFor = (rows: ReturnType<typeof presentTranscript>, cursor: string) =>
    rows.find((row) => row.bodyCursors?.includes(cursor));

  it("a cut row carries its body cursors until the bodies load", () => {
    const rows = presentTranscript({ items: lite.projection.items });
    const row = rowFor(rows, "cursor-3");
    expect(row?.bodyCursors?.slice(0, 2)).toEqual(["cursor-3", "cursor-4"]);
    expect(row?.bodyState).toBe("preview");

    const loading = presentTranscript({ items: lite.projection.items, liteBodies: { loading: new Set(["cursor-3"]) } });
    expect(rowFor(loading, "cursor-3")?.bodyState).toBe("loading");

    const failed = presentTranscript({ items: lite.projection.items, liteBodies: { failed: new Set(["cursor-3"]) } });
    expect(rowFor(failed, "cursor-3")?.bodyState).toBe("failed");

    const gone = presentTranscript({
      items: lite.projection.items,
      liteBodies: { unavailable: new Set(row?.bodyCursors ?? []) },
    });
    expect(rowFor(gone, "cursor-3")?.bodyState).toBe("unavailable");
  });

  it("a loaded body replaces the preview and drops the cursor", () => {
    const whole = "the whole output";
    const rows = presentTranscript({
      items: lite.projection.items,
      liteBodies: {
        bodies: new Map([
          ["cursor-3", { id: "evt-3", cursor: "cursor-3", tool_input_json: { command: "rg -n" } } as never],
          ["cursor-4", { id: "evt-4", cursor: "cursor-4", tool_output_text: whole } as never],
        ]),
      },
    });
    expect(rowFor(rows, "cursor-3")).toBeUndefined();
    expect(rowFor(rows, "cursor-4")).toBeUndefined();
    const outputs = rows.flatMap((row) => [row.output, ...row.calls.map((call) => call.output)]);
    expect(outputs).toContain(whole);
  });
});
