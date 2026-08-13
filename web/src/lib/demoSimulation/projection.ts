import type {
  AgentEvent,
  AgentSessionProjectionItem,
  AgentSessionProjectionResponse,
} from "../../services/api/agents";
import type { DemoSessionEvent, DemoStory } from "./types";

const SESSION_ID = "landing-demo-simulated";
const BASE_TIME = Date.parse("2026-01-15T09:41:00.000Z");

function timestamp(t: number): string {
  return new Date(BASE_TIME + Math.round(t * 1000)).toISOString();
}

function agentEvent(
  event: DemoSessionEvent,
  fields: Pick<AgentEvent, "role" | "content_text" | "tool_name" | "tool_input_json" | "tool_output_text" | "tool_call_id">,
): AgentSessionProjectionItem {
  const at = timestamp(event.t);
  return {
    kind: "event",
    session_id: SESSION_ID,
    timestamp: at,
    event: {
      id: event.id,
      ...fields,
      tool_call_state: fields.role === "assistant" && fields.tool_name ? "completed" : null,
      timestamp: at,
      in_active_context: true,
      is_head_branch: true,
    },
  };
}

function toolOutput(output: string, failed: boolean): string {
  return `Chunk ID: demo\nWall time: 0.10 seconds\nProcess exited with code ${failed ? 1 : 0}\nOutput:\n${output}\n`;
}

export function demoStoryToProjection(story: DemoStory): AgentSessionProjectionResponse {
  const items = story.events.flatMap((event): AgentSessionProjectionItem[] => {
    if (event.type === "instruction_received") {
      return [agentEvent(event, {
        role: "user",
        content_text: event.prompt,
        tool_name: null,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
      })];
    }
    if (event.type === "assistant_text") {
      return [agentEvent(event, {
        role: "assistant",
        content_text: event.text,
        tool_name: null,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
      })];
    }
    if (event.type === "tool_started") {
      return [agentEvent(event, {
        role: "assistant",
        content_text: null,
        tool_name: event.tool,
        tool_input_json: event.input,
        tool_output_text: null,
        tool_call_id: event.callId,
      })];
    }
    if (event.type === "tool_result") {
      return [agentEvent(event, {
        role: "tool",
        content_text: null,
        tool_name: event.tool,
        tool_input_json: null,
        tool_output_text: toolOutput(event.output, event.failed ?? false),
        tool_call_id: event.callId,
      })];
    }
    if (event.type === "completed") {
      return [agentEvent(event, {
        role: "assistant",
        content_text: event.summary,
        tool_name: null,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
      })];
    }
    return [];
  });

  return {
    root_session_id: SESSION_ID,
    focus_session_id: SESSION_ID,
    head_session_id: SESSION_ID,
    path_session_ids: [SESSION_ID],
    items,
    total: items.length,
    branch_mode: "head",
  };
}
