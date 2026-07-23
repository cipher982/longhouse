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
        "Read": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan),
        "Edit": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand),
        "Write": ToolTierMeta(tier: .action, aggregate: nil, icon: "W", label: "Write", color: .success),
        "NotebookEdit": ToolTierMeta(tier: .action, aggregate: nil, icon: "N", label: "Notebook", color: .brand),
        "Bash": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Bash", color: .warning),
        "Task": ToolTierMeta(tier: .action, aggregate: nil, icon: "T", label: "Task", color: .secondary),
        "Agent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary),
        "Grep": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Grep", color: .muted),
        "Glob": ToolTierMeta(tier: .noise, aggregate: .list, icon: "*", label: "Glob", color: .muted),
        "LS": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "List", color: .muted),
        "ToolSearch": ToolTierMeta(tier: .noise, aggregate: .search, icon: "?", label: "ToolSearch", color: .muted),
        "TodoRead": ToolTierMeta(tier: .noise, aggregate: nil, icon: "=", label: "TodoRead", color: .muted),
        "TodoWrite": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "TodoWrite", color: .accent),
        "WebFetch": ToolTierMeta(tier: .context, aggregate: nil, icon: "W", label: "Fetch", color: .cyan),
        "WebSearch": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Search", color: .secondary),
        "read_file": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "read_file", color: .cyan),
        "grep": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "grep", color: .muted),
        "list_files": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "list", color: .muted),
        "find": ToolTierMeta(tier: .noise, aggregate: .search, icon: "?", label: "find", color: .muted),
        "codebase_search": ToolTierMeta(tier: .noise, aggregate: .search, icon: "?", label: "search", color: .muted),
        "web_search": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "web_search", color: .secondary),
        "shell": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "shell", color: .warning),
        "shell_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "shell", color: .warning),
        "exec_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "exec", color: .warning),
        "run_shell_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "shell", color: .warning),
        "write_stdin": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "stdin", color: .warning),
        "apply_patch": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "patch", color: .brand),
        "create_file": ToolTierMeta(tier: .action, aggregate: nil, icon: "W", label: "create", color: .success),
        "str_replace_editor": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "edit", color: .brand),
        "update_plan": ToolTierMeta(tier: .action, aggregate: nil, icon: "+", label: "plan", color: .accent),
        "exec": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "exec", color: .warning),
        "wait": ToolTierMeta(tier: .action, aggregate: nil, icon: "…", label: "Wait", color: .tertiary),
        "request_user_input": ToolTierMeta(tier: .action, aggregate: nil, icon: "?", label: "Question", color: .accent),
        "ReadFile": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan),
        "StrReplace": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand),
        "AwaitShell": ToolTierMeta(tier: .action, aggregate: nil, icon: "…", label: "Wait", color: .tertiary),
        "view_file": ToolTierMeta(tier: .context, aggregate: .read, icon: "R", label: "Read", color: .cyan),
        "grep_search": ToolTierMeta(tier: .noise, aggregate: .search, icon: "~", label: "Search", color: .muted),
        "run_command": ToolTierMeta(tier: .action, aggregate: nil, icon: "$", label: "Shell", color: .warning),
        "list_dir": ToolTierMeta(tier: .noise, aggregate: .list, icon: "/", label: "List", color: .muted),
        "search_web": ToolTierMeta(tier: .context, aggregate: nil, icon: "S", label: "Search", color: .secondary),
        "replace_file_content": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand),
        "multi_replace_file_content": ToolTierMeta(tier: .action, aggregate: nil, icon: "E", label: "Edit", color: .brand),
        "write_to_file": ToolTierMeta(tier: .action, aggregate: nil, icon: "W", label: "Write", color: .success),
        "read_url_content": ToolTierMeta(tier: .context, aggregate: nil, icon: "W", label: "Fetch", color: .cyan),
        "invoke_subagent": ToolTierMeta(tier: .action, aggregate: nil, icon: "A", label: "Agent", color: .tertiary),
    ]

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
                                    mcpNamespace: mcp.namespace)
                }
            }
            return Resolved(tier: mcpDefaultTier, aggregate: mcpDefaultAggregate, icon: "M",
                            label: mcp.method, color: .muted,
                            mcpNamespace: mcp.namespace)
        }
        if let exact = tools[name] {
            return Resolved(tier: exact.tier, aggregate: exact.aggregate, icon: exact.icon,
                            label: exact.label, color: exact.color, mcpNamespace: nil)
        }
        let lower = name.lowercased()
        for (key, meta) in tools where key.lowercased() == lower {
            return Resolved(tier: meta.tier, aggregate: meta.aggregate, icon: meta.icon,
                            label: meta.label, color: meta.color, mcpNamespace: nil)
        }
        let fallbackIcon = String(name.first.map { String($0).uppercased() } ?? " ")
        return Resolved(tier: defaultTier, aggregate: defaultAggregate, icon: fallbackIcon,
                        label: name, color: .muted, mcpNamespace: nil)
    }

    public static func tier(_ name: String) -> ToolTier {
        resolve(name).tier
    }

    public static func aggregate(_ name: String) -> ToolAggregate? {
        resolve(name).aggregate
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
    public static let shellTools: Set<String> = ["Bash", "shell", "shell_command", "exec_command", "run_shell_command"]
    public static let readOnlyCommands: Set<String> = ["grep", "rg", "ls", "cat", "head", "tail", "nl", "wc", "stat", "which", "echo", "pwd", "du", "df", "ps", "printenv", "whoami", "tree", "diff", "column", "uniq", "jq", "basename", "dirname", "type", "true", "man"]
    public static let gitReadSubcommands: Set<String> = ["status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog"]
    public static let aggregateByHead: [String: ToolAggregate] = ["grep": .search, "rg": .search, "ls": .list, "tree": .list, "du": .list, "df": .list]
    public static let defaultReadAggregate: ToolAggregate = .read
}
