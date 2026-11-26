/**
 * Exploration-run projection helpers — membership, summary copy, overflow.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  buildTimelineModel,
  EXPLORATION_OVERFLOW_VISIBLE,
  formatActivitySummary,
  getToolSummary,
  isActivityEligible,
  splitExplorationOverflow,
  type ToolInteraction,
} from "..";
import type { AgentEvent, AgentSessionProjectionItem } from "@/shared/api/agents";

function event(partial: Partial<AgentEvent> & Pick<AgentEvent, "id" | "role" | "timestamp">): AgentEvent {
  return {
    content_text: null,
    tool_name: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    in_active_context: true,
    is_head_branch: true,
    ...partial,
  } as AgentEvent;
}

function projection(events: AgentEvent[]): AgentSessionProjectionItem[] {
  return events.map((evt) => ({
    kind: "event" as const,
    session_id: "s1",
    timestamp: evt.timestamp,
    event: evt,
  }));
}

function interaction(partial: Partial<ToolInteraction> & Pick<ToolInteraction, "toolName" | "key" | "anchorId">): ToolInteraction {
  return {
    callEvent: null,
    resultEvent: { id: 99, role: "tool", timestamp: "2026-01-01T00:00:00Z" } as AgentEvent,
    pairing: "id",
    timestamp: "2026-01-01T00:00:00Z",
    ...partial,
  };
}

type SummaryFixtureCall = {
  category: "read" | "edit" | "run" | "wait";
  operations?: Array<{ key: string; label: string; count: number }>;
  /** Raw tool input, so edit cases can carry file paths and diff shapes. */
  input?: Record<string, unknown>;
};

const summaryFixtures = JSON.parse(
  readFileSync(resolve(process.cwd(), "../config/shell-activity-summary-fixtures.json"), "utf8"),
) as { cases: Array<{ name: string; calls: SummaryFixtureCall[]; expected: string }> };

function fixtureInteraction(call: SummaryFixtureCall, index: number): ToolInteraction {
  const toolName = call.category === "read" ? "Read"
    : call.category === "edit" ? "Edit"
      : call.category === "wait" ? "write_stdin"
        : "shell";
  return interaction({
    key: `fixture-${index}`,
    anchorId: index,
    toolName,
    callEvent: {
      id: index,
      role: "assistant",
      timestamp: "2026-01-01T00:00:00Z",
      tool_name: toolName,
      tool_input_json: call.input ?? null,
    } as AgentEvent,
    presentation: {
      version: 2,
      disposition: "direct",
      tool_name: toolName,
      source_tool_name: toolName,
      execution_method: null,
      label: toolName,
      icon: "$",
      color: "tertiary",
      tier: "noise",
      aggregate: call.category === "wait" ? "wait" : null,
      mcp_namespace: null,
      tool_input_json: {},
      rule_id: "fixture",
      wrapper_recedes: false,
      children: [],
      shell_summary: call.operations ? {
        version: 1,
        confidence: "syntactic",
        operations: call.operations.map((operation) => ({ ...operation, executable: operation.label.split(" ")[0], subcommands: [] })),
        candidate_count: call.operations.length,
        truncated: false,
        dynamic: false,
        parse_error: null,
        parser_id: "fixture",
        shape_registry_version: 1,
      } : null,
    },
  });
}

describe("exploration run helpers", () => {
  it.each(summaryFixtures.cases)("matches the shared shell summary contract: $name", ({ calls, expected }) => {
    expect(formatActivitySummary(calls.map(fixtureInteraction))).toBe(expected);
  });

  it("renders the served call text and never re-derives it from arguments", () => {
    const served = interaction({
      toolName: "Bash",
      key: "served",
      anchorId: 2,
      callEvent: event({
        id: 2,
        role: "assistant",
        timestamp: "2026-01-01T00:00:00Z",
        tool_name: "Bash",
        tool_input_json: { command: "docker ps -a", i: "ignored by the client" },
        tool_intent: "  Listing running containers  ",
        tool_intent_source: "authored",
        tool_target: "docker ps -a",
      }),
    });
    expect(getToolSummary(served)).toBe("Listing running containers");

    expect(getToolSummary(interaction({
      toolName: "Bash",
      key: "unserved",
      anchorId: 3,
      callEvent: event({
        id: 3,
        role: "assistant",
        timestamp: "2026-01-01T00:00:00Z",
        tool_name: "Bash",
        tool_input_json: { command: "docker ps -a", i: "a purpose the server did not serve" },
      }),
    }))).toBe("");
  });

  it("formats semantic verb counts in fixed order", () => {
    expect(
      formatActivitySummary([
        interaction({ key: "1", anchorId: 1, toolName: "Glob" }),
        interaction({ key: "2", anchorId: 2, toolName: "Read" }),
        interaction({ key: "3", anchorId: 3, toolName: "Grep" }),
        interaction({ key: "4", anchorId: 4, toolName: "Grep" }),
        interaction({ key: "5", anchorId: 5, toolName: "LS" }),
      ]),
    ).toBe("Searched 2 searches, read 1 file, listed 2 directories");
  });

  it("splits overflow keeping the latest visible window", () => {
    const items = Array.from({ length: EXPLORATION_OVERFLOW_VISIBLE + 3 }, (_, i) => i);
    const { earlier, latest } = splitExplorationOverflow(items);
    expect(earlier).toEqual([0, 1, 2]);
    expect(latest).toHaveLength(EXPLORATION_OVERFLOW_VISIBLE);
    expect(latest[0]).toBe(3);
  });

  it("includes completed web calls but excludes pending calls", () => {
    expect(
      isActivityEligible(
        interaction({
          key: "w",
          anchorId: 1,
          toolName: "WebFetch",
        }),
      ),
    ).toBe(true);
    expect(
      isActivityEligible(
        interaction({
          key: "p",
          anchorId: 2,
          toolName: "Grep",
          pairing: "pending",
          resultEvent: null,
        }),
      ),
    ).toBe(false);
  });

  it("keeps questions and explicit failures out of activity runs", () => {
    expect(isActivityEligible(interaction({
      key: "question",
      anchorId: 1,
      toolName: "request_user_input",
    }))).toBe(false);
    expect(isActivityEligible(interaction({
      key: "claude-question",
      anchorId: 2,
      toolName: "AskUserQuestion",
    }))).toBe(false);
    expect(isActivityEligible(interaction({
      key: "failure",
      anchorId: 3,
      toolName: "Edit",
      resultEvent: event({
        id: 100,
        role: "tool",
        timestamp: "2026-01-01T00:00:01Z",
        tool_output_text: '{"ok":false,"error":"denied"}',
      }),
    }))).toBe(false);
  });
});

describe("exploration run integration", () => {
  it("replaces Codex polling wrappers with one concise wait group", () => {
    const waitPresentation = {
      version: 1,
      disposition: "parsed" as const,
      tool_name: "write_stdin",
      source_tool_name: "exec",
      execution_method: "exec",
      label: "Wait",
      icon: "…",
      color: "tertiary",
      tier: "noise" as const,
      aggregate: "wait" as const,
      mcp_namespace: null,
      tool_input_json: { session_id: 42, chars: "" },
      rule_id: "codex:exec:single-child:v1",
      wrapper_recedes: true,
      children: [],
    };
    const events: AgentEvent[] = [];
    for (let index = 0; index < 6; index += 1) {
      const callId = `wait-${index}`;
      events.push(
        event({
          id: index * 2 + 1,
          role: "assistant",
          tool_name: "exec",
          tool_call_id: callId,
          tool_input_json: "const r=await tools.write_stdin(...); text(r);",
          tool_presentation: waitPresentation,
          timestamp: `2026-01-01T00:00:${String(index * 2).padStart(2, "0")}Z`,
        }),
        event({
          id: index * 2 + 2,
          role: "tool",
          tool_call_id: callId,
          tool_output_text: "still running",
          timestamp: `2026-01-01T00:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
        }),
      );
    }

    const model = buildTimelineModel(projection(events));

    expect(model.items).toHaveLength(1);
    expect(model.items[0].kind).toBe("activity_group");
    expect(formatActivitySummary(model.activityGroups[0].interactions)).toBe("Waited 6 times");

    const failed = model.activityGroups[0].interactions[0];
    failed.resultEvent = event({
      id: 99,
      role: "tool",
      tool_output_text: "Process exited with code 1\nOutput:\nfailed",
      timestamp: "2026-01-01T00:01:00Z",
    });
    expect(isActivityEligible(failed)).toBe(false);
  });

  it("collapses mixed completed work into one activity run", () => {
    const model = buildTimelineModel(
      projection([
        event({ id: 1, role: "user", content_text: "go", timestamp: "2026-01-01T00:00:00Z" }),
        event({
          id: 2,
          role: "assistant",
          tool_name: "Read",
          tool_call_id: "r1",
          tool_input_json: { file_path: "/a.ts" },
          timestamp: "2026-01-01T00:00:01Z",
        }),
        event({
          id: 3,
          role: "tool",
          tool_call_id: "r1",
          tool_output_text: "a",
          timestamp: "2026-01-01T00:00:02Z",
        }),
        event({
          id: 4,
          role: "assistant",
          tool_name: "Grep",
          tool_call_id: "g1",
          tool_input_json: { pattern: "x" },
          timestamp: "2026-01-01T00:00:03Z",
        }),
        event({
          id: 5,
          role: "tool",
          tool_call_id: "g1",
          tool_output_text: "hit",
          timestamp: "2026-01-01T00:00:04Z",
        }),
        event({
          id: 6,
          role: "assistant",
          tool_name: "Edit",
          tool_call_id: "e1",
          tool_input_json: { file_path: "/a.ts" },
          timestamp: "2026-01-01T00:00:05Z",
        }),
        event({
          id: 7,
          role: "tool",
          tool_call_id: "e1",
          tool_output_text: "ok",
          timestamp: "2026-01-01T00:00:06Z",
        }),
        event({
          id: 8,
          role: "assistant",
          content_text: "Done.",
          timestamp: "2026-01-01T00:00:07Z",
        }),
      ]),
    );

    expect(model.items.map((item) => item.kind)).toEqual([
      "message",
      "activity_group",
      "message",
    ]);
    const group = model.activityGroups[0];
    expect(group.interactions.map((i) => i.toolName)).toEqual(["Read", "Grep", "Edit"]);
    // Named, not counted: an input with a path but no diff shape still names
    // the file so the reader sees *what* changed without expanding.
    expect(formatActivitySummary(group.interactions)).toBe("Searched 1 search, read 1 file, edited a.ts");
  });

  it("keeps an alternating edit/read refactor in one group", () => {
    // The reason grouping was preserved rather than split on mutations: pulling
    // edits into their own rows makes every homogeneous run length one, and a
    // refactor degenerates to one row per call.
    const events = [
      event({ id: 1, role: "user", content_text: "refactor", timestamp: "2026-01-01T00:00:00Z" }),
    ];
    let id = 2;
    for (let i = 0; i < 4; i++) {
      const readId = `r${i}`;
      events.push(event({
        id: id++, role: "assistant", tool_name: "Read", tool_call_id: readId,
        tool_input_json: { file_path: `/f${i}.ts` },
        timestamp: `2026-01-01T00:01:${String(i * 4).padStart(2, "0")}Z`,
      }));
      events.push(event({
        id: id++, role: "tool", tool_call_id: readId, tool_output_text: "body",
        timestamp: `2026-01-01T00:01:${String(i * 4 + 1).padStart(2, "0")}Z`,
      }));
      const editId = `e${i}`;
      events.push(event({
        id: id++, role: "assistant", tool_name: "Edit", tool_call_id: editId,
        tool_input_json: { file_path: `/f${i}.ts`, old_string: "a", new_string: "b" },
        timestamp: `2026-01-01T00:01:${String(i * 4 + 2).padStart(2, "0")}Z`,
      }));
      events.push(event({
        id: id++, role: "tool", tool_call_id: editId, tool_output_text: "ok",
        timestamp: `2026-01-01T00:01:${String(i * 4 + 3).padStart(2, "0")}Z`,
      }));
    }

    const model = buildTimelineModel(projection(events));

    // 8 tool calls collapse to one group, not 8 rows.
    expect(model.items.map((item) => item.kind)).toEqual(["message", "activity_group"]);
    expect(model.activityGroups).toHaveLength(1);
    expect(model.activityGroups[0].interactions).toHaveLength(8);
    expect(formatActivitySummary(model.activityGroups[0].interactions)).toBe(
      "Read 4 files, edited f0.ts +1 −1 · f1.ts +1 −1 · +2 more",
    );
  });

  it("keeps a singleton Read as an individual context row", () => {
    const model = buildTimelineModel(
      projection([
        event({ id: 1, role: "user", content_text: "read", timestamp: "2026-01-01T00:00:00Z" }),
        event({
          id: 2,
          role: "assistant",
          tool_name: "Read",
          tool_call_id: "r1",
          timestamp: "2026-01-01T00:00:01Z",
        }),
        event({
          id: 3,
          role: "tool",
          tool_call_id: "r1",
          tool_output_text: "ok",
          timestamp: "2026-01-01T00:00:02Z",
        }),
      ]),
    );
    expect(model.items.map((item) => item.kind)).toEqual(["message", "tool"]);
    expect(model.activityGroups).toHaveLength(0);
  });

  it("treats co-located assistant prose as a run boundary", () => {
    const model = buildTimelineModel(
      projection([
        event({ id: 1, role: "user", content_text: "go", timestamp: "2026-01-01T00:00:00Z" }),
        event({
          id: 2,
          role: "assistant",
          tool_name: "Grep",
          tool_call_id: "g1",
          tool_input_json: { pattern: "a" },
          timestamp: "2026-01-01T00:00:01Z",
        }),
        event({
          id: 3,
          role: "tool",
          tool_call_id: "g1",
          tool_output_text: "a",
          timestamp: "2026-01-01T00:00:02Z",
        }),
        event({
          id: 4,
          role: "assistant",
          content_text: "Next I will search again.",
          tool_name: "Grep",
          tool_call_id: "g2",
          tool_input_json: { pattern: "b" },
          timestamp: "2026-01-01T00:00:03Z",
        }),
        event({
          id: 5,
          role: "tool",
          tool_call_id: "g2",
          tool_output_text: "b",
          timestamp: "2026-01-01T00:00:04Z",
        }),
        event({
          id: 6,
          role: "assistant",
          tool_name: "Grep",
          tool_call_id: "g3",
          tool_input_json: { pattern: "c" },
          timestamp: "2026-01-01T00:00:05Z",
        }),
        event({
          id: 7,
          role: "tool",
          tool_call_id: "g3",
          tool_output_text: "c",
          timestamp: "2026-01-01T00:00:06Z",
        }),
      ]),
    );
    expect(model.items.map((item) => item.kind)).toEqual([
      "message",
      "tool",
      "message",
      "activity_group",
    ]);
    expect(model.activityGroups[0].interactions.map((i) => i.toolName)).toEqual(["Grep", "Grep"]);
  });
});

describe("withheld thoughts between calls", () => {
  // Claude Opus writes an empty, withheld thought before nearly every call.
  // The pane shows one "Reasoning withheld" per turn, so the hidden ones must
  // not split a run of finished calls into a row each.
  it("groups calls separated only by empty withheld thoughts", () => {
    const events: AgentEvent[] = [];
    let id = 1;
    const at = () => `2026-01-01T00:00:${String(id).padStart(2, "0")}Z`;
    for (let index = 0; index < 4; index += 1) {
      events.push(
        event({ id, role: "assistant", content_text: "", reasoning_withheld: true, item: { kind: "reasoning", visibility: "withheld" }, timestamp: at() } as Partial<AgentEvent> & Pick<AgentEvent, "id" | "role" | "timestamp">),
      );
      id += 1;
      events.push(event({ id, role: "assistant", tool_name: "Bash", tool_call_id: `c${index}`, tool_input_json: { command: `ls ${index}` }, timestamp: at() }));
      id += 1;
      events.push(event({ id, role: "tool", tool_call_id: `c${index}`, tool_output_text: "ok", timestamp: at() }));
      id += 1;
    }

    const model = buildTimelineModel(projection(events));

    // The first thought leads the run and stays (the pane's one marker); the
    // ones between calls fold away.
    expect(model.items.map((item) => item.kind)).toEqual(["reasoning", "activity_group"]);
    expect(model.activityGroups[0].interactions).toHaveLength(4);
  });

  it("keeps a thought with text as a break between calls", () => {
    const events: AgentEvent[] = [
      event({ id: 1, role: "assistant", tool_name: "Bash", tool_call_id: "a", tool_input_json: { command: "ls" }, timestamp: "2026-01-01T00:00:01Z" }),
      event({ id: 2, role: "tool", tool_call_id: "a", tool_output_text: "ok", timestamp: "2026-01-01T00:00:02Z" }),
      event({ id: 3, role: "assistant", content_text: "Now check the frames.", item: { kind: "reasoning" }, timestamp: "2026-01-01T00:00:03Z" } as Partial<AgentEvent> & Pick<AgentEvent, "id" | "role" | "timestamp">),
      event({ id: 4, role: "assistant", tool_name: "Bash", tool_call_id: "b", tool_input_json: { command: "ls" }, timestamp: "2026-01-01T00:00:04Z" }),
      event({ id: 5, role: "tool", tool_call_id: "b", tool_output_text: "ok", timestamp: "2026-01-01T00:00:05Z" }),
    ];
    expect(buildTimelineModel(projection(events)).items.map((item) => item.kind)).toEqual(["tool", "reasoning", "tool"]);
  });
});
