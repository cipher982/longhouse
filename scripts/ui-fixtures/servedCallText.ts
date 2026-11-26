import type { AgentEvent } from "../../web/src/shared/api/agents";

/**
 * Fixture-side stand-in for the server's `resolve_call_text`
 * (server/zerg/services/tool_presentation.py): the call text a real API
 * serves on every tool call. Clients never derive it, so a hand-built
 * fixture event must carry it. Covers the shapes these fixtures use; the
 * server is the authority.
 */
const PATH_KEYS = ["file_path", "path", "filePath", "Path", "AbsolutePath", "target_file", "notebook_path"];
const AUTHORED_KEYS = ["i", "toolSummary"];
const DESCRIPTION_TOOLS = new Set(["bash", "shell", "shell_command", "exec_command", "run_shell_command", "run_terminal_cmd", "task", "agent", "monitor"]);
const SEARCH_TOOLS = new Set(["grep", "glob", "search", "rg", "grep_search", "codebase_search", "find"]);
const WEB_TOOLS = new Set(["webfetch", "websearch", "web_search", "fetch", "read_url_content", "search_web"]);
const AGENT_TOOLS = new Set(["task", "agent", "spawn_agent", "subagent"]);
const NO_TEXT_TOOLS = new Set(["todowrite", "update_plan", "wait", "write_stdin", "wait_agent", "askuserquestion"]);

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function firstLine(value: string): string | null {
  return value.split("\n").map((line) => line.split(/\s+/).filter(Boolean).join(" ")).find(Boolean) ?? null;
}

function pathTail(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length >= 2 ? parts.slice(-2).join("/") : (parts[0] ?? path);
}

export function servedCallText(
  toolName: string,
  rawInput: unknown,
): Pick<AgentEvent, "tool_intent" | "tool_intent_source" | "tool_target"> {
  const none = { tool_intent: null, tool_intent_source: null, tool_target: null };
  const name = toolName.toLowerCase();
  if (typeof rawInput === "string") {
    const files = [...rawInput.matchAll(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/gm)].map((m) => m[1]!.trim());
    if (!files.length) return none;
    const extra = new Set(files).size - 1;
    const first = pathTail(files[0]!);
    return { tool_intent: extra ? `${first} + ${extra} ${extra === 1 ? "file" : "files"}` : first, tool_intent_source: "derived", tool_target: files.join(", ") };
  }
  if (!rawInput || typeof rawInput !== "object" || name.startsWith("mcp__")) return none;
  const input = rawInput as Record<string, unknown>;
  const command = text(input.command) ?? text(input.cmd) ?? text(input.CommandLine);
  const path = PATH_KEYS.map((key) => text(input[key])).find(Boolean) ?? null;
  const pattern = text(input.pattern) ?? text(input.query) ?? text(input.glob);
  const url = text(input.url);
  const target = command ?? path ?? pattern ?? url;
  const authored = AUTHORED_KEYS.map((key) => text(input[key])).find(Boolean)
    ?? (DESCRIPTION_TOOLS.has(name) ? text(input.description) : null);
  if (authored) return { tool_intent: authored, tool_intent_source: "authored", tool_target: target };
  if (NO_TEXT_TOOLS.has(name)) return { ...none, tool_target: target };
  let derived: string | null = null;
  if (command) derived = firstLine(command);
  else if (SEARCH_TOOLS.has(name)) derived = pattern ?? path;
  else if (WEB_TOOLS.has(name)) derived = url ?? pattern;
  else if (path) derived = pathTail(path);
  else if (AGENT_TOOLS.has(name)) derived = firstLine(text(input.prompt) ?? "");
  else derived = pattern ?? url;
  return derived ? { tool_intent: derived, tool_intent_source: "derived", tool_target: target } : { ...none, tool_target: target };
}
