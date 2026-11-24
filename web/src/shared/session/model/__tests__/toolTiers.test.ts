import { describe, expect, it } from "vitest";
import { resolveToolInfo } from "../toolTiers.generated";
import { buildTimelineModel, getToolSummary } from "../timelineModel";
import { presentTranscript } from "../presentTranscript";
import type { AgentSessionProjectionItem } from "@/shared/api/agents";
import type { SubagentChild } from "../types";

describe("exact tool aliases", () => {
  it("always translates known native names", () => {
    expect(resolveToolInfo("view_file").label).toBe("Read");
  });

  it("keeps unknown provider tools raw", () => {
    expect(resolveToolInfo("CallDynamicTool").label).toBe("CallDynamicTool");
  });

  it("classifies Antigravity swarm tools cleanly", () => {
    expect(resolveToolInfo("swarm")).toMatchObject({ tier: "action", aggregate: null, label: "Swarm" });
    expect(resolveToolInfo("post_message")).toMatchObject({ tier: "action", aggregate: null, label: "Board" });
    expect(resolveToolInfo("read_board")).toMatchObject({ tier: "noise", aggregate: "read", label: "Board" });
    expect(resolveToolInfo("swarm_wait")).toMatchObject({ tier: "noise", aggregate: "wait", label: "Wait" });
    expect(resolveToolInfo("swarm_note")).toMatchObject({ tier: "action", aggregate: null, label: "Swarm" });
  });
});

describe("swarm tool summaries and subagent binding", () => {
  it("summarizes JSON-quoted compact args and ignores MCP-namespaced collisions", () => {
    const items: AgentSessionProjectionItem[] = [
      {
        kind: "event",
        session_id: "coord-quoted",
        timestamp: "2026-05-21T22:27:42Z",
        event: {
          id: 101,
          role: "assistant",
          tool_name: "swarm",
          tool_call_id: "antigravity-1-0",
          tool_input_json: { action: '"add"', name: '"worker-a"', workspace: '"share"' },
          tool_output_text: null,
          content_text: null,
          timestamp: "2026-05-21T22:27:42Z",
        },
      },
      {
        kind: "event",
        session_id: "coord-quoted",
        timestamp: "2026-05-21T22:27:43Z",
        event: {
          id: 102,
          role: "assistant",
          tool_name: "post_message",
          tool_call_id: "antigravity-2-0",
          tool_input_json: { text: '"@worker-a First line\\nSecond line"' },
          tool_output_text: null,
          content_text: null,
          timestamp: "2026-05-21T22:27:43Z",
        },
      },
      {
        kind: "event",
        session_id: "coord-quoted",
        timestamp: "2026-05-21T22:27:44Z",
        event: {
          id: 103,
          role: "assistant",
          tool_name: "mcp__slack__post_message",
          tool_call_id: "mcp-1",
          tool_input_json: { text: "Hello channel", channel: "#general" },
          tool_output_text: null,
          content_text: null,
          timestamp: "2026-05-21T22:27:44Z",
        },
      },
    ];
    const model = buildTimelineModel(items);
    const [quotedAdd, quotedPost, mcpPost] = model.toolItems;
    expect(getToolSummary(quotedAdd)).toBe("add worker-a (share)");
    expect(getToolSummary(quotedPost)).toBe("@worker-a First line");
    expect(getToolSummary(mcpPost)).toBe("");
  });

  it("summarizes swarm, post_message, read_board, and swarm_note without raw JSON", () => {
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
    const [swarmAdd, postMsg, readBoard] = model.toolItems;
    expect(getToolSummary(swarmAdd)).toBe("add data-worker (share)");
    expect(swarmAdd.children?.map((c) => c.session_id)).toEqual(["child-session-1"]);
    expect(getToolSummary(postMsg)).toBe("@data-worker Status check?");
    expect(getToolSummary(readBoard)).toBe("since #40");

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
