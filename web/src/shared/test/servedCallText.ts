import type { AgentEvent } from "@/shared/api/agents";

/**
 * The call text the server serves for a shell call (`resolve_call_text` in
 * server/zerg/services/tool_presentation.py), for hand-built test events: the
 * agent's description when it wrote one, else the command's first line.
 * Clients never derive this themselves; fixtures carry it like the API does.
 */
export function servedShellText(
  input: Record<string, unknown>,
): Pick<AgentEvent, "tool_intent" | "tool_intent_source" | "tool_target"> {
  const command = typeof input.command === "string" ? input.command.trim() : "";
  const description = typeof input.description === "string" ? input.description.trim() : "";
  const firstLine = command.split("\n").map((line) => line.trim()).find(Boolean) ?? "";
  if (description) return { tool_intent: description, tool_intent_source: "authored", tool_target: command || null };
  if (firstLine) return { tool_intent: firstLine, tool_intent_source: "derived", tool_target: command };
  return { tool_intent: null, tool_intent_source: null, tool_target: null };
}
