import type { SessionEvent } from "./sessionEvents";

export interface LiveSessionEvent {
  id: string;
  role: "user" | "assistant" | "tool";
  content_text?: string;
  tool_name?: string;
  tool_input_json?: Record<string, unknown>;
  tool_output_text?: string;
  tool_call_id?: string;
}

function stringInput(input: Record<string, unknown> | undefined, key: string): string | undefined {
  const value = input?.[key];
  return typeof value === "string" ? value : undefined;
}

function basename(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

function inputSummary(event: LiveSessionEvent): string | undefined {
  const input = event.tool_input_json;
  const tool = event.tool_name ?? "";
  if (["Bash", "shell", "shell_command", "exec_command", "run_shell_command"].includes(tool)) {
    return (stringInput(input, "command") ?? stringInput(input, "cmd"))?.split("\n")[0];
  }
  if (["Read", "Edit", "Write", "Update", "NotebookEdit"].includes(tool)) {
    const path = stringInput(input, "file_path") ?? stringInput(input, "path");
    return path ? basename(path) : undefined;
  }
  if (["Grep", "Glob"].includes(tool)) return stringInput(input, "pattern");
  if (tool === "Task") {
    return (stringInput(input, "description") ?? stringInput(input, "prompt"))?.split("\n")[0];
  }
  return stringInput(input, "query") ?? stringInput(input, "url");
}

function resultSummary(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const lines = value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length) return undefined;
  return lines.at(-1);
}

/** Pair the same normalized call/result events consumed by Longhouse clients. */
export function projectLiveSessionEvents(events: LiveSessionEvent[]): SessionEvent[] {
  const results = new Map(
    events
      .filter((event) => event.role === "tool" && event.tool_call_id)
      .map((event) => [event.tool_call_id!, resultSummary(event.tool_output_text)]),
  );

  return events.flatMap((event, index): SessionEvent[] => {
    if (event.role !== "assistant") return [];
    if (event.content_text?.trim()) {
      return [{ id: event.id, tSec: index, kind: "assistant", title: event.content_text }];
    }
    if (!event.tool_name) return [];
    return [{
      id: event.id,
      tSec: index,
      kind: "tool",
      title: event.tool_name,
      subtitle: inputSummary(event),
      result: event.tool_call_id ? results.get(event.tool_call_id) : undefined,
    }];
  });
}
