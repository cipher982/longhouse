// @generated from config/tool-tiers.json — do not edit by hand.
// Run: python3 scripts/generate/tool_tiers.py

export type ToolTier = "noise" | "context" | "action";
export type ToolAggregate = "search" | "read" | "list" | "wait";
export type ToolColorToken =
  | "brand" | "cyan" | "success" | "warning" | "secondary"
  | "tertiary" | "accent" | "muted";

export interface ToolTierMeta {
  tier: ToolTier;
  /** When set, completed calls may join consecutive exploration runs. */
  aggregate: ToolAggregate | null;
  icon: string;
  /** Canonical label from config/tool-tiers.json `labels` (Shell, Read, Edit, ...). */
  label: string;
  color: ToolColorToken;
  /** Present-tense phrase for a status line while the tool runs ("Running a command"). */
  status: string;
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
  "Bash": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "shell": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "shell_command": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "exec_command": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "run_shell_command": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "run_terminal_cmd": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "run_command": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "exec": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "local_shell_call": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running a command" },
  "write_stdin": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Sending input to a command" },
  "eval": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Running code" },
  "Monitor": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Watching a command" },
  "BashOutput": { tier: "noise", aggregate: "wait", icon: "\u2026", label: "Shell", color: "tertiary", status: "Reading command output" },
  "KillShell": { tier: "action", aggregate: null, icon: "$", label: "Shell", color: "warning", status: "Stopping a command" },
  "Read": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "read_file": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "ReadFile": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "view_file": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "read_many_files": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "view_file_outline": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "view_code_item": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "view_image": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Viewing an image" },
  "NotebookRead": { tier: "context", aggregate: "read", icon: "R", label: "Read", color: "cyan", status: "Reading files" },
  "Edit": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "MultiEdit": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "StrReplace": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "str_replace_editor": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "search_replace": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "replace": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "edit_file": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "apply_patch": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "replace_file_content": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "multi_replace_file_content": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Editing files" },
  "delete": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Deleting a file" },
  "delete_file": { tier: "action", aggregate: null, icon: "E", label: "Edit", color: "brand", status: "Deleting a file" },
  "NotebookEdit": { tier: "action", aggregate: null, icon: "N", label: "Notebook", color: "brand", status: "Editing a notebook" },
  "Write": { tier: "action", aggregate: null, icon: "W", label: "Write", color: "success", status: "Writing a file" },
  "write_file": { tier: "action", aggregate: null, icon: "W", label: "Write", color: "success", status: "Writing a file" },
  "write_to_file": { tier: "action", aggregate: null, icon: "W", label: "Write", color: "success", status: "Writing a file" },
  "create_file": { tier: "action", aggregate: null, icon: "W", label: "Write", color: "success", status: "Writing a file" },
  "Grep": { tier: "noise", aggregate: "search", icon: "~", label: "Search", color: "muted", status: "Searching" },
  "find": { tier: "noise", aggregate: "search", icon: "~", label: "Search", color: "muted", status: "Searching" },
  "codebase_search": { tier: "noise", aggregate: "search", icon: "~", label: "Search", color: "muted", status: "Searching" },
  "grep_search": { tier: "noise", aggregate: "search", icon: "~", label: "Search", color: "muted", status: "Searching" },
  "search_content": { tier: "noise", aggregate: "search", icon: "~", label: "Search", color: "muted", status: "Searching" },
  "search_file_content": { tier: "noise", aggregate: "search", icon: "~", label: "Search", color: "muted", status: "Searching" },
  "recall": { tier: "noise", aggregate: "search", icon: "~", label: "Search", color: "muted", status: "Searching past sessions" },
  "Glob": { tier: "noise", aggregate: "list", icon: "*", label: "List", color: "muted", status: "Listing files" },
  "LS": { tier: "noise", aggregate: "list", icon: "/", label: "List", color: "muted", status: "Listing files" },
  "list_files": { tier: "noise", aggregate: "list", icon: "/", label: "List", color: "muted", status: "Listing files" },
  "list": { tier: "noise", aggregate: "list", icon: "/", label: "List", color: "muted", status: "Listing files" },
  "list_dir": { tier: "noise", aggregate: "list", icon: "/", label: "List", color: "muted", status: "Listing files" },
  "list_directory": { tier: "noise", aggregate: "list", icon: "/", label: "List", color: "muted", status: "Listing files" },
  "find_by_name": { tier: "noise", aggregate: "list", icon: "/", label: "List", color: "muted", status: "Listing files" },
  "WebFetch": { tier: "context", aggregate: null, icon: "W", label: "Fetch", color: "cyan", status: "Fetching a page" },
  "web_fetch": { tier: "context", aggregate: null, icon: "W", label: "Fetch", color: "cyan", status: "Fetching a page" },
  "fetch": { tier: "context", aggregate: null, icon: "W", label: "Fetch", color: "cyan", status: "Fetching a page" },
  "read_url_content": { tier: "context", aggregate: null, icon: "W", label: "Fetch", color: "cyan", status: "Fetching a page" },
  "WebSearch": { tier: "context", aggregate: null, icon: "S", label: "Web search", color: "secondary", status: "Searching the web" },
  "web_search": { tier: "context", aggregate: null, icon: "S", label: "Web search", color: "secondary", status: "Searching the web" },
  "web_search_call": { tier: "context", aggregate: null, icon: "S", label: "Web search", color: "secondary", status: "Searching the web" },
  "search_web": { tier: "context", aggregate: null, icon: "S", label: "Web search", color: "secondary", status: "Searching the web" },
  "google_web_search": { tier: "context", aggregate: null, icon: "S", label: "Web search", color: "secondary", status: "Searching the web" },
  "web__run": { tier: "context", aggregate: null, icon: "S", label: "Web search", color: "secondary", status: "Searching the web" },
  "Agent": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running a subagent" },
  "Task": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running a subagent" },
  "subagent": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running a subagent" },
  "invoke_subagent": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running a subagent" },
  "spawn_agent": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running a subagent" },
  "manage_subagents": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Managing subagents" },
  "followup_task": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Messaging a subagent" },
  "send_message": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Messaging a subagent" },
  "SendMessage": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Messaging a subagent" },
  "list_agents": { tier: "noise", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Checking subagents" },
  "ListAgents": { tier: "noise", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Checking subagents" },
  "interrupt_agent": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Stopping a subagent" },
  "close_agent": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Stopping a subagent" },
  "Workflow": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running a workflow" },
  "run_workflow": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running a workflow" },
  "swarm": { tier: "action", aggregate: null, icon: "A", label: "Agent", color: "tertiary", status: "Running subagents" },
  "swarm_note": { tier: "action", aggregate: null, icon: "+", label: "Agent", color: "accent", status: "Updating the board" },
  "post_message": { tier: "action", aggregate: null, icon: "B", label: "Agent", color: "secondary", status: "Posting to the board" },
  "read_board": { tier: "noise", aggregate: "read", icon: "B", label: "Agent", color: "muted", status: "Reading the board" },
  "TodoWrite": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating the plan" },
  "TodoRead": { tier: "noise", aggregate: null, icon: "=", label: "Plan", color: "muted", status: "Reading the plan" },
  "update_plan": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating the plan" },
  "todo": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating the plan" },
  "update_steps": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating the plan" },
  "manage_task": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating tasks" },
  "TaskCreate": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating tasks" },
  "TaskUpdate": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating tasks" },
  "TaskList": { tier: "noise", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Reading tasks" },
  "TaskGet": { tier: "noise", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Reading tasks" },
  "enter_plan_mode": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Planning" },
  "exit_plan_mode": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Finishing the plan" },
  "EnterPlanMode": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Planning" },
  "ExitPlanMode": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Finishing the plan" },
  "goal": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating the goal" },
  "create_goal": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating the goal" },
  "update_goal": { tier: "action", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Updating the goal" },
  "get_goal": { tier: "noise", aggregate: null, icon: "+", label: "Plan", color: "accent", status: "Reading the goal" },
  "AskUserQuestion": { tier: "action", aggregate: null, icon: "?", label: "Ask", color: "accent", status: "Asking a question" },
  "request_user_input": { tier: "action", aggregate: null, icon: "?", label: "Ask", color: "accent", status: "Asking a question" },
  "AskQuestion": { tier: "action", aggregate: null, icon: "?", label: "Ask", color: "accent", status: "Asking a question" },
  "question": { tier: "action", aggregate: null, icon: "?", label: "Ask", color: "accent", status: "Asking a question" },
  "ask": { tier: "action", aggregate: null, icon: "?", label: "Ask", color: "accent", status: "Asking a question" },
  "wait": { tier: "noise", aggregate: "wait", icon: "\u2026", label: "Wait", color: "tertiary", status: "Waiting" },
  "AwaitShell": { tier: "action", aggregate: null, icon: "\u2026", label: "Wait", color: "tertiary", status: "Waiting" },
  "swarm_wait": { tier: "noise", aggregate: "wait", icon: "\u2026", label: "Wait", color: "tertiary", status: "Waiting" },
  "wait_agent": { tier: "noise", aggregate: "wait", icon: "\u2026", label: "Wait", color: "tertiary", status: "Waiting on a subagent" },
  "schedule": { tier: "action", aggregate: null, icon: "\u2026", label: "Wait", color: "tertiary", status: "Scheduling a wake-up" },
  "ScheduleWakeup": { tier: "action", aggregate: null, icon: "\u2026", label: "Wait", color: "tertiary", status: "Scheduling a wake-up" },
  "StructuredOutput": { tier: "action", aggregate: null, icon: "\u2713", label: "Result", color: "success", status: "Returning a result" },
  "yield": { tier: "action", aggregate: null, icon: "\u2713", label: "Result", color: "success", status: "Returning a result" },
  "ToolSearch": { tier: "noise", aggregate: "search", icon: "?", label: "Tool", color: "muted", status: "Finding tools" },
  "tool_search": { tier: "noise", aggregate: "search", icon: "?", label: "Tool", color: "muted", status: "Finding tools" },
  "tool_search_call": { tier: "noise", aggregate: "search", icon: "?", label: "Tool", color: "muted", status: "Finding tools" },
  "GetDynamicTools": { tier: "noise", aggregate: "search", icon: "?", label: "Tool", color: "muted", status: "Finding tools" },
  "Skill": { tier: "action", aggregate: null, icon: "K", label: "Tool", color: "muted", status: "Loading a skill" },
  "CallDynamicTool": { tier: "action", aggregate: null, icon: "T", label: "Tool", color: "muted", status: "Using a tool" },
  "TaskStop": { tier: "action", aggregate: null, icon: "x", label: "Tool", color: "muted", status: "Stopping a task" },
  "Artifact": { tier: "action", aggregate: null, icon: "T", label: "Tool", color: "muted", status: "Publishing an artifact" },
};

/** Lowercased tool name -> entry; lookup is case-insensitive on every surface. */
const TOOL_TIERS_BY_LOWER: Record<string, ToolTierMeta> = Object.fromEntries(
  Object.entries(TOOL_TIERS).map(([name, meta]) => [name.toLowerCase(), meta]),
);

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
  status: string;
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
          status: `Using ${mcp.method.replace(/_/g, " ")}`,
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
      status: `Using ${mcp.method.replace(/_/g, " ")}`,
      mcpNamespace: mcp.namespace,
    };
  }

  const known = TOOL_TIERS[toolName] ?? TOOL_TIERS_BY_LOWER[toolName.toLowerCase()];
  if (known) return { ...known };

  return {
    tier: DEFAULT_TOOL_TIER,
    aggregate: DEFAULT_TOOL_AGGREGATE,
    icon: (toolName[0] || " ").toUpperCase(),
    label: toolName,
    color: "muted",
    status: `Using ${toolName}`,
  };
}

export function toolTier(toolName: string): ToolTier {
  return resolveToolInfo(toolName).tier;
}

export function toolAggregate(toolName: string): ToolAggregate | null {
  return resolveToolInfo(toolName).aggregate;
}

/**
 * Tools whose call input IS the agent's final answer (schema-constrained
 * returns). Timeline builders render the payload as the closing assistant
 * message instead of an opaque tool row.
 */
export const FINAL_ANSWER_TOOLS: ReadonlySet<string> = new Set(["StructuredOutput"]);

export function isFinalAnswerTool(toolName: string): boolean {
  const lower = toolName.toLowerCase();
  for (const name of FINAL_ANSWER_TOOLS) {
    if (name.toLowerCase() === lower) return true;
  }
  return false;
}

// --- Shell classifier constants (grammar is handwritten in shellSalience.ts;
// parity with Swift is enforced by config/shell-salience-fixtures.json). ---

/** Lowercased; compare with `name.toLowerCase()`. */
export const SHELL_TOOLS: ReadonlySet<string> = new Set(["bash", "shell", "shell_command", "exec_command", "run_shell_command", "run_terminal_cmd", "run_command", "exec", "local_shell_call"]);
export const SHELL_READ_ONLY_COMMANDS: ReadonlySet<string> = new Set(["grep", "rg", "ls", "cat", "head", "tail", "nl", "wc", "stat", "which", "echo", "pwd", "du", "df", "ps", "printenv", "whoami", "tree", "diff", "column", "uniq", "jq", "basename", "dirname", "type", "true", "man"]);
export const SHELL_GIT_READ_SUBCOMMANDS: ReadonlySet<string> = new Set(["status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog"]);
export const SHELL_AGGREGATE_BY_HEAD: Record<string, ToolAggregate> = {"grep": "search", "rg": "search", "ls": "list", "tree": "list", "du": "list", "df": "list"};
export const SHELL_DEFAULT_READ_AGGREGATE: ToolAggregate = "read";
