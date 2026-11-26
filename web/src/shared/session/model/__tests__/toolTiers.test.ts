import { describe, expect, it } from "vitest";
import { resolveToolInfo } from "../toolTiers.generated";
import { buildTimelineModel } from "../timelineModel";
import { presentTranscript } from "../presentTranscript";
import type { AgentSessionProjectionItem } from "@/shared/api/agents";
import type { SubagentChild } from "../types";

describe("exact tool aliases", () => {
  it("always translates known native names", () => {
    expect(resolveToolInfo("view_file").label).toBe("Read");
  });

  it("keeps unknown provider tools raw", () => {
    expect(resolveToolInfo("mystery_tool")).toMatchObject({ label: "mystery_tool", status: "Using mystery_tool" });
  });

  it("matches every provider's spelling case-insensitively to one canonical label", () => {
    for (const name of ["Bash", "bash", "Shell", "exec_command", "run_command"]) {
      expect(resolveToolInfo(name)).toMatchObject({ label: "Shell", status: "Running a command" });
    }
    expect(resolveToolInfo("read_file")).toMatchObject({ label: "Read", status: "Reading files" });
    expect(resolveToolInfo("edit_file").label).toBe("Edit");
    expect(resolveToolInfo("mcp__trove__search_personal_memory")).toMatchObject({
      label: "search_personal_memory",
      status: "Using search personal memory",
    });
  });

  it("classifies Antigravity swarm tools as Agent", () => {
    expect(resolveToolInfo("swarm")).toMatchObject({ tier: "action", aggregate: null, label: "Agent" });
    expect(resolveToolInfo("post_message")).toMatchObject({ tier: "action", aggregate: null, label: "Agent" });
    expect(resolveToolInfo("read_board")).toMatchObject({ tier: "noise", aggregate: "read", label: "Agent" });
    expect(resolveToolInfo("swarm_wait")).toMatchObject({ tier: "noise", aggregate: "wait", label: "Wait" });
    expect(resolveToolInfo("swarm_note")).toMatchObject({ tier: "action", aggregate: null, label: "Agent" });
  });
});

describe("swarm subagent binding", () => {
  it("binds swarm workers to their child sessions", () => {
    const items: AgentSessionProjectionItem[] = [
      {
        kind: "event",
        session_id: "coord-1",
        timestamp: "2026-05-21T22:27:42Z",
        event: {
          id: 1,
          role: "assistant",
          tool_name: "swarm",
          tool_call_id: "antigravity-20-1",
          tool_input_json: { action: "add", name: "data-worker", workspace: "share", brief: "Own truth store" },
          tool_output_text: null,
          content_text: null,
          timestamp: "2026-05-21T22:27:42Z",
        },
      },
      {
        kind: "event",
        session_id: "coord-1",
        timestamp: "2026-05-21T22:27:43Z",
        event: {
          id: 2,
          role: "tool",
          tool_name: "swarm",
          tool_call_id: "antigravity-20-1",
          tool_input_json: null,
          tool_output_text: '{"status":"added","name":"data-worker","conversation_id":"child-native-1"}',
          content_text: null,
          timestamp: "2026-05-21T22:27:43Z",
        },
      },
      {
        kind: "event",
        session_id: "coord-1",
        timestamp: "2026-05-21T22:28:00Z",
        event: {
          id: 3,
          role: "assistant",
          tool_name: "post_message",
          tool_call_id: "antigravity-21-0",
          tool_input_json: { text: "@data-worker Status check?\nSecond line ignored." },
          tool_output_text: null,
          content_text: null,
          timestamp: "2026-05-21T22:28:00Z",
        },
      },
      {
        kind: "event",
        session_id: "coord-1",
        timestamp: "2026-05-21T22:28:01Z",
        event: {
          id: 4,
          role: "tool",
          tool_name: "post_message",
          tool_call_id: "antigravity-21-0",
          tool_input_json: null,
          tool_output_text: '{"status":"posted","seq":40}',
          content_text: null,
          timestamp: "2026-05-21T22:28:01Z",
        },
      },
      {
        kind: "event",
        session_id: "coord-1",
        timestamp: "2026-05-21T22:28:10Z",
        event: {
          id: 5,
          role: "assistant",
          tool_name: "read_board",
          tool_call_id: "antigravity-22-0",
          tool_input_json: { since: 40 },
          tool_output_text: null,
          content_text: null,
          timestamp: "2026-05-21T22:28:10Z",
        },
      },
      {
        kind: "event",
        session_id: "coord-1",
        timestamp: "2026-05-21T22:28:11Z",
        event: {
          id: 6,
          role: "tool",
          tool_name: "read_board",
          tool_call_id: "antigravity-22-0",
          tool_input_json: null,
          tool_output_text: '{"messages":[]}',
          content_text: null,
          timestamp: "2026-05-21T22:28:11Z",
        },
      },
    ];

    const subagents: SubagentChild[] = [
      {
        session_id: "child-session-1",
        provider: "antigravity",
        provider_session_id: "child-native-1",
        parent_session_id: "coord-1",
        parent_tool_call_id: "antigravity-20-1",
        parent_agent_id: null,
        run_id: null,
        agent_type: "swarm",
        description: "data-worker",
        model: "gemini-2.5-pro",
        is_fork: null,
        spawn_depth: null,
        finished: true,
        live_status: null,
        duration_ms: 12000,
        input_tokens: null,
        output_tokens: null,
        total_tokens: null,
        context_tokens: null,
        context_window: null,
        started_at: "2026-05-21T22:27:43Z",
        last_activity_at: "2026-05-21T22:27:55Z",
        ended_at: "2026-05-21T22:27:55Z",
        user_messages: 1,
        assistant_messages: 1,
        tool_calls: 4,
        title: null,
        first_user_message_preview: null,
        last_visible_text_preview: null,
      },
    ];

    const model = buildTimelineModel(items, subagents);
    expect(model.toolItems).toHaveLength(3);
    const [swarmAdd] = model.toolItems;
    expect(swarmAdd.children?.map((c) => c.session_id)).toEqual(["child-session-1"]);

    const presented = presentTranscript({ items, subagents });
    const withSubagents = presented.find((row) => (row.subagents?.length ?? 0) > 0);
    expect(withSubagents?.subagents).toEqual([
      expect.objectContaining({
        sessionId: "child-session-1",
        label: "data-worker",
        agentType: "swarm",
      }),
    ]);
  });
});
