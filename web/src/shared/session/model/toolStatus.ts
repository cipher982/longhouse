import { resolveToolInfo, TOOL_TIERS } from "./toolTiers.generated";

const TOOL_NAME = /^[A-Za-z_][\w.-]*$/;

/**
 * The status phrase for a tool a provider reports as running ("Running a
 * command", "Reading files"). Some providers put a whole command line or a
 * name followed by JSON arguments in the tool slot (Antigravity); that reads
 * as the tool its first token names, or as a command, never as raw text.
 * Mirrors the server's activity-tool split (tool_presentation.py).
 */
export function toolStatusPhrase(raw: string): string {
  const tool = raw.trim();
  if (TOOL_NAME.test(tool) || tool.startsWith("mcp__")) return resolveToolInfo(tool).status;
  const head = tool.split(/[\s({]/, 1)[0] ?? "";
  if (head && TOOL_NAME.test(head)) {
    const known = TOOL_TIERS[head] ?? Object.entries(TOOL_TIERS).find(([name]) => name.toLowerCase() === head.toLowerCase())?.[1];
    if (known) return known.status;
  }
  return "Running a command";
}
