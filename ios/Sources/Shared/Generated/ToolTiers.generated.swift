// @generated from config/tool-tiers.json — do not edit by hand.
// Run: python3 scripts/generate/tool_tiers.py

import Foundation

public enum ToolTier: String, Sendable {
    case noise
    case context
    case action
}

public enum ToolAggregate: String, Sendable {
    case search
    case read
    case list
    case wait
}

public enum ToolColorToken: String, Sendable {
    case brand, cyan, success, warning, secondary, tertiary, accent, muted
}

public struct ToolTierMeta: Sendable {
    public let tier: ToolTier
    public let aggregate: ToolAggregate?
    public let icon: String
    public let label: String
    public let color: ToolColorToken
    public let status: String
}

public struct McpNamespaceMeta: Sendable {
    public let icon: String
    public let color: ToolColorToken
}

public enum ToolTiers {
    public static let defaultTier: ToolTier = .action
    public static let mcpDefaultTier: ToolTier = .noise
    public static let defaultAggregate: ToolAggregate? = nil
    public static let mcpDefaultAggregate: ToolAggregate? = nil

    public static let tools: [String: ToolTierMeta] = [
        "Bash": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "shell": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "shell_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "exec_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "run_shell_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "run_terminal_cmd": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "run_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "exec": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "local_shell_call": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running a command"),
        "write_stdin": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Sending input to a command"),
        "eval": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Running code"),
        "Monitor": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Watching a command"),
        "BashOutput": ToolTierMeta(tier: .noise, aggregate: .wait, icon: "…", label: "Shell", color: .tertiary, status: "Reading command output"),
        "KillShell": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning, status: "Stopping a command"),
        "Read": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "read_file": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "ReadFile": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "view_file": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "read_many_files": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "view_file_outline": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "view_code_item": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "view_image": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Viewing an image"),
        "NotebookRead": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan, status: "Reading files"),
        "Edit": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "MultiEdit": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "StrReplace": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "str_replace_editor": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "search_replace": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "replace": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "edit_file": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "apply_patch": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "replace_file_content": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "multi_replace_file_content": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Editing files"),
        "delete": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Deleting a file"),
        "delete_file": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand, status: "Deleting a file"),
        "NotebookEdit": ToolTierMeta(tier: .action, aggregate: nil, icon: "N", label: "Notebook", color: .brand, status: "Editing a notebook"),
        "Write": ToolTierMeta(tier: .action, aggregate: nil, icon: "W", label: "Write", color: .success, status: "Writing a file"),
        "write_file": ToolTierMeta(tier: .action, aggregate: nil, icon: "W", label: "Write", color: .success, status: "Writing a file"),
        "write_to_file": ToolTierMeta(tier: .action, aggregate: nil, icon: "W", label: "Write", color: .success, status: "Writing a file"),
        "create_file": ToolTierMeta(tier: .action, aggregate: nil, icon: "W", label: "Write", color: .success, status: "Writing a file"),
        "Grep": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted, status: "Searching"),
        "find": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted, status: "Searching"),
        "codebase_search": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted, status: "Searching"),
        "grep_search": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted, status: "Searching"),
        "search_content": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted, status: "Searching"),
        "search_file_content": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted, status: "Searching"),
        "recall": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted, status: "Searching past sessions"),
        "Glob": ToolTierMeta(tier: .noise, aggregate: .list, icon: "*", label: "List", color: .muted, status: "Listing files"),
        "LS": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "List", color: .muted, status: "Listing files"),
        "list_files": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "List", color: .muted, status: "Listing files"),
        "list_dir": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "List", color: .muted, status: "Listing files"),
        "list_directory": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "List", color: .muted, status: "Listing files"),
        "find_by_name": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "List", color: .muted, status: "Listing files"),
        "WebFetch": ToolTierMeta(tier: .context, aggregate: nil, icon: "W", label: "Fetch", color: .cyan, status: "Fetching a page"),
        "web_fetch": ToolTierMeta(tier: .context, aggregate: nil, icon: "W", label: "Fetch", color: .cyan, status: "Fetching a page"),
        "fetch": ToolTierMeta(tier: .context, aggregate: nil, icon: "W", label: "Fetch", color: .cyan, status: "Fetching a page"),
        "read_url_content": ToolTierMeta(tier: .context, aggregate: nil, icon: "W", label: "Fetch", color: .cyan, status: "Fetching a page"),
        "WebSearch": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Web search", color: .secondary, status: "Searching the web"),
        "web_search": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Web search", color: .secondary, status: "Searching the web"),
        "web_search_call": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Web search", color: .secondary, status: "Searching the web"),
        "search_web": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Web search", color: .secondary, status: "Searching the web"),
        "google_web_search": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Web search", color: .secondary, status: "Searching the web"),
        "web__run": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Web search", color: .secondary, status: "Searching the web"),
        "Agent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running a subagent"),
        "Task": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running a subagent"),
        "subagent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running a subagent"),
        "invoke_subagent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running a subagent"),
        "spawn_agent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running a subagent"),
        "manage_subagents": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Managing subagents"),
        "followup_task": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Messaging a subagent"),
        "send_message": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Messaging a subagent"),
        "SendMessage": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Messaging a subagent"),
        "list_agents": ToolTierMeta(tier: .noise, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Checking subagents"),
        "ListAgents": ToolTierMeta(tier: .noise, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Checking subagents"),
        "interrupt_agent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Stopping a subagent"),
        "close_agent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Stopping a subagent"),
        "Workflow": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running a workflow"),
        "run_workflow": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running a workflow"),
        "swarm": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary, status: "Running subagents"),
        "swarm_note": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Agent", color: .accent, status: "Updating the board"),
        "post_message": ToolTierMeta(tier: .action, aggregate: nil, icon: "B", label: "Agent", color: .secondary, status: "Posting to the board"),
        "read_board": ToolTierMeta(tier: .noise, aggregate: .read, icon: "B", label: "Agent", color: .muted, status: "Reading the board"),
        "TodoWrite": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating the plan"),
        "TodoRead": ToolTierMeta(tier: .noise, aggregate: nil, icon: "=", label: "Plan", color: .muted, status: "Reading the plan"),
        "update_plan": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating the plan"),
        "todo": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating the plan"),
        "update_steps": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating the plan"),
        "manage_task": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating tasks"),
        "TaskCreate": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating tasks"),
        "TaskUpdate": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating tasks"),
        "TaskList": ToolTierMeta(tier: .noise, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Reading tasks"),
        "TaskGet": ToolTierMeta(tier: .noise, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Reading tasks"),
        "enter_plan_mode": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Planning"),
        "exit_plan_mode": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Finishing the plan"),
        "EnterPlanMode": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Planning"),
        "ExitPlanMode": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Finishing the plan"),
        "goal": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating the goal"),
        "create_goal": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating the goal"),
        "update_goal": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Updating the goal"),
        "get_goal": ToolTierMeta(tier: .noise, aggregate: nil, icon: "+", label: "Plan", color: .accent, status: "Reading the goal"),
        "AskUserQuestion": ToolTierMeta(tier: .action, aggregate: nil, icon: "?", label: "Ask", color: .accent, status: "Asking a question"),
        "request_user_input": ToolTierMeta(tier: .action, aggregate: nil, icon: "?", label: "Ask", color: .accent, status: "Asking a question"),
        "AskQuestion": ToolTierMeta(tier: .action, aggregate: nil, icon: "?", label: "Ask", color: .accent, status: "Asking a question"),
        "question": ToolTierMeta(tier: .action, aggregate: nil, icon: "?", label: "Ask", color: .accent, status: "Asking a question"),
        "ask": ToolTierMeta(tier: .action, aggregate: nil, icon: "?", label: "Ask", color: .accent, status: "Asking a question"),
        "wait": ToolTierMeta(tier: .noise, aggregate: .wait, icon: "…", label: "Wait", color: .tertiary, status: "Waiting"),
        "AwaitShell": ToolTierMeta(tier: .action, aggregate: nil, icon: "…", label: "Wait", color: .tertiary, status: "Waiting"),
        "swarm_wait": ToolTierMeta(tier: .noise, aggregate: .wait, icon: "…", label: "Wait", color: .tertiary, status: "Waiting"),
        "wait_agent": ToolTierMeta(tier: .noise, aggregate: .wait, icon: "…", label: "Wait", color: .tertiary, status: "Waiting on a subagent"),
        "schedule": ToolTierMeta(tier: .action, aggregate: nil, icon: "…", label: "Wait", color: .tertiary, status: "Scheduling a wake-up"),
        "ScheduleWakeup": ToolTierMeta(tier: .action, aggregate: nil, icon: "…", label: "Wait", color: .tertiary, status: "Scheduling a wake-up"),
        "StructuredOutput": ToolTierMeta(tier: .action, aggregate: nil, icon: "✓", label: "Result", color: .success, status: "Returning a result"),
        "yield": ToolTierMeta(tier: .action, aggregate: nil, icon: "✓", label: "Result", color: .success, status: "Returning a result"),
        "ToolSearch": ToolTierMeta(tier: .noise, aggregate: .search, icon: "?", label: "Tool", color: .muted, status: "Finding tools"),
        "tool_search": ToolTierMeta(tier: .noise, aggregate: .search, icon: "?", label: "Tool", color: .muted, status: "Finding tools"),
        "tool_search_call": ToolTierMeta(tier: .noise, aggregate: .search, icon: "?", label: "Tool", color: .muted, status: "Finding tools"),
        "GetDynamicTools": ToolTierMeta(tier: .noise, aggregate: .search, icon: "?", label: "Tool", color: .muted, status: "Finding tools"),
        "Skill": ToolTierMeta(tier: .action, aggregate: nil, icon: "K", label: "Tool", color: .muted, status: "Loading a skill"),
        "CallDynamicTool": ToolTierMeta(tier: .action, aggregate: nil, icon: "T", label: "Tool", color: .muted, status: "Using a tool"),
        "TaskStop": ToolTierMeta(tier: .action, aggregate: nil, icon: "x", label: "Tool", color: .muted, status: "Stopping a task"),
        "Artifact": ToolTierMeta(tier: .action, aggregate: nil, icon: "T", label: "Tool", color: .muted, status: "Publishing an artifact"),
    ]

    /// Lowercased tool name -> entry; lookup is case-insensitive on every surface.
    private static let toolsByLower: [String: ToolTierMeta] = Dictionary(
        uniqueKeysWithValues: tools.map { ($0.key.lowercased(), $0.value) }
    )

    public static let mcpNamespaces: [String: McpNamespaceMeta] = [
        "longhouse": McpNamespaceMeta(icon: "O", color: .brand),
        "life-hub": McpNamespaceMeta(icon: "O", color: .brand),
        "browser": McpNamespaceMeta(icon: "B", color: .cyan),
        "search": McpNamespaceMeta(icon: "S", color: .secondary),
        "web": McpNamespaceMeta(icon: "S", color: .secondary),
        "gdrive": McpNamespaceMeta(icon: "G", color: .success),
    ]

    public struct Resolved: Sendable {
        public let tier: ToolTier
        public let aggregate: ToolAggregate?
        public let icon: String
        public let label: String
        public let color: ToolColorToken
        public let status: String
        public let mcpNamespace: String?
    }

    public static func resolve(_ name: String) -> Resolved {
        if let mcp = parseMcp(name) {
            let ns = mcp.namespace.lowercased()
            let parts = Set(ns.split(whereSeparator: { $0 == "-" || $0 == "_" }).map(String.init))
            for (prefix, meta) in mcpNamespaces {
                if ns == prefix || parts.contains(prefix) ||
                   ns.hasPrefix(prefix + "-") || ns.hasPrefix(prefix + "_") {
                    return Resolved(tier: mcpDefaultTier, aggregate: mcpDefaultAggregate,
                                    icon: meta.icon, label: mcp.method, color: meta.color,
                                    status: mcpStatus(mcp.method), mcpNamespace: mcp.namespace)
                }
            }
            return Resolved(tier: mcpDefaultTier, aggregate: mcpDefaultAggregate, icon: "M",
                            label: mcp.method, color: .muted,
                            status: mcpStatus(mcp.method), mcpNamespace: mcp.namespace)
        }
        if let known = tools[name] ?? toolsByLower[name.lowercased()] {
            return Resolved(tier: known.tier, aggregate: known.aggregate, icon: known.icon,
                            label: known.label, color: known.color, status: known.status,
                            mcpNamespace: nil)
        }
        let fallbackIcon = String(name.first.map { String($0).uppercased() } ?? " ")
        return Resolved(tier: defaultTier, aggregate: defaultAggregate, icon: fallbackIcon,
                        label: name, color: .muted, status: "Using \(name)", mcpNamespace: nil)
    }

    private static func mcpStatus(_ method: String) -> String {
        "Using " + method.replacingOccurrences(of: "_", with: " ")
    }

    public static func tier(_ name: String) -> ToolTier {
        resolve(name).tier
    }

    public static func aggregate(_ name: String) -> ToolAggregate? {
        resolve(name).aggregate
    }

    /// Tools whose call input IS the agent's final answer (schema-constrained
    /// returns). Timeline builders render the payload as the closing assistant
    /// message instead of an opaque tool row.
    public static let finalAnswerTools: Set<String> = ["StructuredOutput"]

    public static func isFinalAnswerTool(_ name: String) -> Bool {
        let lower = name.lowercased()
        return finalAnswerTools.contains { $0.lowercased() == lower }
    }

    private static func parseMcp(_ name: String) -> (namespace: String, method: String)? {
        let parts = name.components(separatedBy: "__")
        guard parts.count == 3, parts[0] == "mcp" else { return nil }
        return (parts[1], parts[2])
    }
}

// --- Shell classifier constants (grammar is handwritten in ShellSalience.swift;
// parity with TS is enforced by config/shell-salience-fixtures.json). ---

public enum ShellClassifierConstants {
    /// Lowercased; compare with `name.lowercased()`.
    public static let shellTools: Set<String> = ["bash", "shell", "shell_command", "exec_command", "run_shell_command", "run_terminal_cmd", "run_command", "exec", "local_shell_call"]
    public static let readOnlyCommands: Set<String> = ["grep", "rg", "ls", "cat", "head", "tail", "nl", "wc", "stat", "which", "echo", "pwd", "du", "df", "ps", "printenv", "whoami", "tree", "diff", "column", "uniq", "jq", "basename", "dirname", "type", "true", "man"]
    public static let gitReadSubcommands: Set<String> = ["status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog"]
    public static let aggregateByHead: [String: ToolAggregate] = ["grep": .search, "rg": .search, "ls": .list, "tree": .list, "du": .list, "df": .list]
    public static let defaultReadAggregate: ToolAggregate = .read
}
