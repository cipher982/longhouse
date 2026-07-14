// @generated from config/tool-tiers.json — do not edit by hand.
// Run: python3 scripts/generate/tool_tiers.py

export type ToolTier = "noise" | "context" | "action";
export type ToolAggregate = "search" | "read" | "list";
export type ToolColorToken =
  | "brand" | "cyan" | "success" | "warning" | "secondary"
  | "tertiary" | "accent" | "muted";

export interface ToolTierMeta {
  tier: ToolTier;
  /** When set, completed calls may join consecutive exploration runs. */
  aggregate: ToolAggregate | null;
  icon: string;
  label: string;
  color: ToolColorToken;
}

export interface McpNamespaceMeta {
  icon: string;
  color: ToolColorToken;
}

export const DEFAULT_TOOL_TIER: ToolTier = "action";
export const MCP_DEFAULT_TIER: ToolTier = "noise";
export const DEFAULT_TOOL_AGGREGATE: ToolAggregate | null = null;
export const MCP_DEFAULT_AGGREGATE: ToolAggregate | null = null;

export const TOOL_TIERS: Record<string, ToolTierMeta> = {
  "Read": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan" },
  "Edit": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand" },
  "Write": { tier: "action", aggregate: null, icon: "W", label: "Write", color: "success" },
  "NotebookEdit": { tier: "action", aggregate: null, icon: "N", label: "Notebook", color: "brand" },
  "Bash": { tier: "action", aggregate: null, icon: "$", label: "Bash", color: "warning" },
  "Task": { tier: "action", aggregate: null, icon: "T", label: "Task", color: "secondary" },
  "Agent": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary" },
  "Grep": { tier: "noise", aggregate: "search", icon: "~", label: "Grep", color: "muted" },
  "Glob": { tier: "noise", aggregate: "list", icon: "*", label: "Glob", color: "muted" },
  "LS": { tier: "noise", aggregate: "list", icon: "/", label: "List", color: "muted" },
  "ToolSearch": { tier: "noise", aggregate: "search", icon: "?", label: "ToolSearch", color: "muted" },
  "TodoRead": { tier: "noise", aggregate: null, icon: "=", label: "TodoRead", color: "muted" },
  "TodoWrite": { tier: "action", aggregate: null, icon: "+", label: "TodoWrite", color: "accent" },
  "WebFetch": { tier: "context", aggregate: null, icon: "W", label: "Fetch", color: "cyan" },
  "WebSearch": { tier: "context", aggregate: null, icon: "S", label: "Search", color: "secondary" },
  "read_file": { tier: "context", aggregate: "read", icon: "R", label: "read_file", color: "cyan" },
  "grep": { tier: "noise", aggregate: "search", icon: "~", label: "grep", color: "muted" },
  "list_files": { tier: "noise", aggregate: "list", icon: "/", label: "list", color: "muted" },
  "find": { tier: "noise", aggregate: "search", icon: "?", label: "find", color: "muted" },
  "codebase_search": { tier: "noise", aggregate: "search", icon: "?", label: "search", color: "muted" },
  "web_search": { tier: "context", aggregate: null, icon: "S", label: "web_search", color: "secondary" },
  "shell": { tier: "action", aggregate: null, icon: "$", label: "shell", color: "warning" },
  "shell_command": { tier: "action", aggregate: null, icon: "$", label: "shell", color: "warning" },
  "exec_command": { tier: "action", aggregate: null, icon: "$", label: "exec", color: "warning" },
  "run_shell_command": { tier: "action", aggregate: null, icon: "$", label: "shell", color: "warning" },
  "write_stdin": { tier: "action", aggregate: null, icon: "$", label: "stdin", color: "warning" },
  "apply_patch": { tier: "action", aggregate: null, icon: "E", label: "patch", color: "brand" },
  "create_file": { tier: "action", aggregate: null, icon: "W", label: "create", color: "success" },
  "str_replace_editor": { tier: "action", aggregate: null, icon: "E", label: "edit", color: "brand" },
  "update_plan": { tier: "action", aggregate: null, icon: "+", label: "plan", color: "accent" },
};

export const MCP_NAMESPACES: Record<string, McpNamespaceMeta> = {
  "longhouse": { icon: "O", color: "brand" },
  "life-hub": { icon: "O", color: "brand" },
  "browser": { icon: "B", color: "cyan" },
  "search": { icon: "S", color: "secondary" },
  "web": { icon: "S", color: "secondary" },
  "gdrive": { icon: "G", color: "success" },
};

const COLOR_TOKEN_TO_CSS: Record<ToolColorToken, string> = {
  brand:     "var(--color-brand-primary)",
  cyan:      "var(--color-neon-cyan)",
  success:   "var(--color-intent-success)",
  warning:   "var(--color-intent-warning)",
  secondary: "var(--color-neon-secondary)",
  tertiary:  "var(--color-text-tertiary)",
  accent:    "var(--color-brand-accent)",
  muted:     "var(--color-text-secondary)",
};

export function colorTokenToCss(token: ToolColorToken): string {
  return COLOR_TOKEN_TO_CSS[token];
}

function parseMcp(name: string): { namespace: string; method: string } | null {
  const parts = name.split("__");
  if (parts.length === 3 && parts[0] === "mcp") {
    return { namespace: parts[1], method: parts[2] };
  }
  return null;
}

export interface ResolvedToolInfo {
  tier: ToolTier;
  aggregate: ToolAggregate | null;
  icon: string;
  label: string;
  color: ToolColorToken;
  mcpNamespace?: string;
}

export function resolveToolInfo(toolName: string): ResolvedToolInfo {
  const mcp = parseMcp(toolName);
  if (mcp) {
    const ns = mcp.namespace.toLowerCase();
    // Exact match first, then word-boundary prefix (e.g. "longhouse-channel"
    // matches "longhouse", but "webhook" doesn't match "web").
    const nsParts = ns.split(/[-_]/);
    for (const [prefix, meta] of Object.entries(MCP_NAMESPACES)) {
      if (ns === prefix || nsParts.includes(prefix) || ns.startsWith(prefix + "-") || ns.startsWith(prefix + "_")) {
        return {
          tier: MCP_DEFAULT_TIER,
          aggregate: MCP_DEFAULT_AGGREGATE,
          icon: meta.icon,
          label: mcp.method,
          color: meta.color,
          mcpNamespace: mcp.namespace,
        };
      }
    }
    return {
      tier: MCP_DEFAULT_TIER,
      aggregate: MCP_DEFAULT_AGGREGATE,
      icon: "M",
      label: mcp.method,
      color: "muted",
      mcpNamespace: mcp.namespace,
    };
  }

  const exact = TOOL_TIERS[toolName];
  if (exact) return { ...exact };

  const lower = toolName.toLowerCase();
  for (const [key, meta] of Object.entries(TOOL_TIERS)) {
    if (key.toLowerCase() === lower) return { ...meta };
  }

  return {
    tier: DEFAULT_TOOL_TIER,
    aggregate: DEFAULT_TOOL_AGGREGATE,
    icon: (toolName[0] || " ").toUpperCase(),
    label: toolName,
    color: "muted",
  };
}

export function toolTier(toolName: string): ToolTier {
  return resolveToolInfo(toolName).tier;
}

export function toolAggregate(toolName: string): ToolAggregate | null {
  return resolveToolInfo(toolName).aggregate;
}
