#!/usr/bin/env python3
"""Generate TypeScript and Swift tool-tier modules from config/tool-tiers.json.

Single source of truth: config/tool-tiers.json.
Outputs:
  - web/src/shared/session/model/toolTiers.generated.ts
  - ios/Sources/Shared/Generated/ToolTiers.generated.swift
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config" / "tool-tiers.json"
TS_OUT = REPO / "web" / "src" / "shared" / "session" / "model" / "toolTiers.generated.ts"
SWIFT_OUT = REPO / "ios" / "Sources" / "Shared" / "Generated" / "ToolTiers.generated.swift"


def load() -> dict:
    data = json.loads(CONFIG.read_text())
    validate(data)
    return data


def validate(data: dict) -> None:
    """Every entry speaks the canonical vocabulary; no two names differ only in case."""
    labels = data.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise ValueError("config/tool-tiers.json: `labels` must map each canonical label to its status phrase")
    seen: dict[str, str] = {}
    for name, meta in data["tools"].items():
        if meta.get("label") not in labels:
            raise ValueError(f"tool {name!r}: label {meta.get('label')!r} is not a canonical label {sorted(labels)}")
        status = meta.get("status")
        if not isinstance(status, str) or not status.strip():
            raise ValueError(f"tool {name!r}: missing `status` phrase")
        key = name.lower()
        if key in seen:
            raise ValueError(f"tools {seen[key]!r} and {name!r} differ only in case; lookup is case-insensitive, keep one")
        seen[key] = name


def _aggregate_literal(value: object) -> str:
    if value is None:
        return "null"
    if value not in {"search", "read", "list", "wait"}:
        raise ValueError(f"unsupported aggregate category: {value!r}")
    return json.dumps(value)


def render_ts(data: dict) -> str:
    tools = data["tools"]
    final_answer_tools_json = json.dumps(data.get("final_answer_tools", []))
    shell = data["shell_classifier"]
    mcp_ns = data["mcp_namespaces"]
    default_tier = data["default_tier"]
    mcp_default_tier = data["mcp_default_tier"]
    default_aggregate = data.get("default_aggregate", None)
    mcp_default_aggregate = data.get("mcp_default_aggregate", None)

    def quote(v: str) -> str:
        return json.dumps(v)

    entries = []
    for name, meta in tools.items():
        aggregate = meta.get("aggregate", default_aggregate)
        entries.append(
            f'  {quote(name)}: {{ tier: {quote(meta["tier"])}, aggregate: {_aggregate_literal(aggregate)}, '
            f'icon: {quote(meta["icon"])}, label: {quote(meta["label"])}, color: {quote(meta["color"])}, '
            f'status: {quote(meta["status"])} }},'
        )

    mcp_entries = []
    for ns, meta in mcp_ns.items():
        mcp_entries.append(
            f'  {quote(ns)}: {{ icon: {quote(meta["icon"])}, color: {quote(meta["color"])} }},'
        )

    return f"""// @generated from config/tool-tiers.json — do not edit by hand.
// Run: python3 scripts/generate/tool_tiers.py

export type ToolTier = "noise" | "context" | "action";
export type ToolAggregate = "search" | "read" | "list" | "wait";
export type ToolColorToken =
  | "brand" | "cyan" | "success" | "warning" | "secondary"
  | "tertiary" | "accent" | "muted";

export interface ToolTierMeta {{
  tier: ToolTier;
  /** When set, completed calls may join consecutive exploration runs. */
  aggregate: ToolAggregate | null;
  icon: string;
  /** Canonical label from config/tool-tiers.json `labels` (Shell, Read, Edit, ...). */
  label: string;
  color: ToolColorToken;
  /** Present-tense phrase for a status line while the tool runs ("Running a command"). */
  status: string;
}}

export interface McpNamespaceMeta {{
  icon: string;
  color: ToolColorToken;
}}

export const DEFAULT_TOOL_TIER: ToolTier = {quote(default_tier)};
export const MCP_DEFAULT_TIER: ToolTier = {quote(mcp_default_tier)};
export const DEFAULT_TOOL_AGGREGATE: ToolAggregate | null = {_aggregate_literal(default_aggregate)};
export const MCP_DEFAULT_AGGREGATE: ToolAggregate | null = {_aggregate_literal(mcp_default_aggregate)};

export const TOOL_TIERS: Record<string, ToolTierMeta> = {{
{chr(10).join(entries)}
}};

/** Lowercased tool name -> entry; lookup is case-insensitive on every surface. */
const TOOL_TIERS_BY_LOWER: Record<string, ToolTierMeta> = Object.fromEntries(
  Object.entries(TOOL_TIERS).map(([name, meta]) => [name.toLowerCase(), meta]),
);

export const MCP_NAMESPACES: Record<string, McpNamespaceMeta> = {{
{chr(10).join(mcp_entries)}
}};

const COLOR_TOKEN_TO_CSS: Record<ToolColorToken, string> = {{
  brand:     "var(--color-brand-primary)",
  cyan:      "var(--color-neon-cyan)",
  success:   "var(--color-intent-success)",
  warning:   "var(--color-intent-warning)",
  secondary: "var(--color-neon-secondary)",
  tertiary:  "var(--color-text-tertiary)",
  accent:    "var(--color-brand-accent)",
  muted:     "var(--color-text-secondary)",
}};

export function colorTokenToCss(token: ToolColorToken): string {{
  return COLOR_TOKEN_TO_CSS[token];
}}

function parseMcp(name: string): {{ namespace: string; method: string }} | null {{
  const parts = name.split("__");
  if (parts.length === 3 && parts[0] === "mcp") {{
    return {{ namespace: parts[1], method: parts[2] }};
  }}
  return null;
}}

export interface ResolvedToolInfo {{
  tier: ToolTier;
  aggregate: ToolAggregate | null;
  icon: string;
  label: string;
  color: ToolColorToken;
  status: string;
  mcpNamespace?: string;
}}

export function resolveToolInfo(toolName: string): ResolvedToolInfo {{
  const mcp = parseMcp(toolName);
  if (mcp) {{
    const ns = mcp.namespace.toLowerCase();
    // Exact match first, then word-boundary prefix (e.g. "longhouse-channel"
    // matches "longhouse", but "webhook" doesn't match "web").
    const nsParts = ns.split(/[-_]/);
    for (const [prefix, meta] of Object.entries(MCP_NAMESPACES)) {{
      if (ns === prefix || nsParts.includes(prefix) || ns.startsWith(prefix + "-") || ns.startsWith(prefix + "_")) {{
        return {{
          tier: MCP_DEFAULT_TIER,
          aggregate: MCP_DEFAULT_AGGREGATE,
          icon: meta.icon,
          label: mcp.method,
          color: meta.color,
          status: `Using ${{mcp.method.replace(/_/g, " ")}}`,
          mcpNamespace: mcp.namespace,
        }};
      }}
    }}
    return {{
      tier: MCP_DEFAULT_TIER,
      aggregate: MCP_DEFAULT_AGGREGATE,
      icon: "M",
      label: mcp.method,
      color: "muted",
      status: `Using ${{mcp.method.replace(/_/g, " ")}}`,
      mcpNamespace: mcp.namespace,
    }};
  }}

  const known = TOOL_TIERS[toolName] ?? TOOL_TIERS_BY_LOWER[toolName.toLowerCase()];
  if (known) return {{ ...known }};

  return {{
    tier: DEFAULT_TOOL_TIER,
    aggregate: DEFAULT_TOOL_AGGREGATE,
    icon: (toolName[0] || " ").toUpperCase(),
    label: toolName,
    color: "muted",
    status: `Using ${{toolName}}`,
  }};
}}

export function toolTier(toolName: string): ToolTier {{
  return resolveToolInfo(toolName).tier;
}}

export function toolAggregate(toolName: string): ToolAggregate | null {{
  return resolveToolInfo(toolName).aggregate;
}}

/**
 * Tools whose call input IS the agent's final answer (schema-constrained
 * returns). Timeline builders render the payload as the closing assistant
 * message instead of an opaque tool row.
 */
export const FINAL_ANSWER_TOOLS: ReadonlySet<string> = new Set({final_answer_tools_json});

export function isFinalAnswerTool(toolName: string): boolean {{
  const lower = toolName.toLowerCase();
  for (const name of FINAL_ANSWER_TOOLS) {{
    if (name.toLowerCase() === lower) return true;
  }}
  return false;
}}

// --- Shell classifier constants (grammar is handwritten in shellSalience.ts;
// parity with Swift is enforced by config/shell-salience-fixtures.json). ---

/** Lowercased; compare with `name.toLowerCase()`. */
export const SHELL_TOOLS: ReadonlySet<string> = new Set({json.dumps([t.lower() for t in shell["shell_tools"]])});
export const SHELL_READ_ONLY_COMMANDS: ReadonlySet<string> = new Set({json.dumps(shell["read_only_commands"])});
export const SHELL_GIT_READ_SUBCOMMANDS: ReadonlySet<string> = new Set({json.dumps(shell["git_read_subcommands"])});
export const SHELL_AGGREGATE_BY_HEAD: Record<string, ToolAggregate> = {json.dumps(shell["aggregate_by_head"])};
export const SHELL_DEFAULT_READ_AGGREGATE: ToolAggregate = {json.dumps(shell["default_read_aggregate"])};
"""


def render_swift(data: dict) -> str:
    tools = data["tools"]
    final_answer_tools_swift = ", ".join(f'"{t}"' for t in data.get("final_answer_tools", []))
    shell = data["shell_classifier"]
    mcp_ns = data["mcp_namespaces"]
    default_aggregate = data.get("default_aggregate", None)
    mcp_default_aggregate = data.get("mcp_default_aggregate", None)

    def swift_aggregate(value: object) -> str:
        if value is None:
            return "nil"
        return f".{value}"

    tool_entries = []
    for name, meta in tools.items():
        aggregate = meta.get("aggregate", default_aggregate)
        tool_entries.append(
            f'        "{name}": ToolTierMeta(tier: .{meta["tier"]}, '
            f'aggregate: {swift_aggregate(aggregate)}, '
            f'icon: "{meta["icon"]}", label: "{meta["label"]}", color: .{meta["color"]}, '
            f'status: "{meta["status"]}"),'
        )

    mcp_entries = []
    for ns, meta in mcp_ns.items():
        mcp_entries.append(
            f'        "{ns}": McpNamespaceMeta(icon: "{meta["icon"]}", color: .{meta["color"]}),'
        )

    return f"""// @generated from config/tool-tiers.json — do not edit by hand.
// Run: python3 scripts/generate/tool_tiers.py

import Foundation

public enum ToolTier: String, Sendable {{
    case noise
    case context
    case action
}}

public enum ToolAggregate: String, Sendable {{
    case search
    case read
    case list
    case wait
}}

public enum ToolColorToken: String, Sendable {{
    case brand, cyan, success, warning, secondary, tertiary, accent, muted
}}

public struct ToolTierMeta: Sendable {{
    public let tier: ToolTier
    public let aggregate: ToolAggregate?
    public let icon: String
    public let label: String
    public let color: ToolColorToken
    public let status: String
}}

public struct McpNamespaceMeta: Sendable {{
    public let icon: String
    public let color: ToolColorToken
}}

public enum ToolTiers {{
    public static let defaultTier: ToolTier = .{data["default_tier"]}
    public static let mcpDefaultTier: ToolTier = .{data["mcp_default_tier"]}
    public static let defaultAggregate: ToolAggregate? = {swift_aggregate(default_aggregate)}
    public static let mcpDefaultAggregate: ToolAggregate? = {swift_aggregate(mcp_default_aggregate)}

    public static let tools: [String: ToolTierMeta] = [
{chr(10).join(tool_entries)}
    ]

    /// Lowercased tool name -> entry; lookup is case-insensitive on every surface.
    private static let toolsByLower: [String: ToolTierMeta] = Dictionary(
        uniqueKeysWithValues: tools.map {{ ($0.key.lowercased(), $0.value) }}
    )

    public static let mcpNamespaces: [String: McpNamespaceMeta] = [
{chr(10).join(mcp_entries)}
    ]

    public struct Resolved: Sendable {{
        public let tier: ToolTier
        public let aggregate: ToolAggregate?
        public let icon: String
        public let label: String
        public let color: ToolColorToken
        public let status: String
        public let mcpNamespace: String?
    }}

    public static func resolve(_ name: String) -> Resolved {{
        if let mcp = parseMcp(name) {{
            let ns = mcp.namespace.lowercased()
            let parts = Set(ns.split(whereSeparator: {{ $0 == "-" || $0 == "_" }}).map(String.init))
            for (prefix, meta) in mcpNamespaces {{
                if ns == prefix || parts.contains(prefix) ||
                   ns.hasPrefix(prefix + "-") || ns.hasPrefix(prefix + "_") {{
                    return Resolved(tier: mcpDefaultTier, aggregate: mcpDefaultAggregate,
                                    icon: meta.icon, label: mcp.method, color: meta.color,
                                    status: mcpStatus(mcp.method), mcpNamespace: mcp.namespace)
                }}
            }}
            return Resolved(tier: mcpDefaultTier, aggregate: mcpDefaultAggregate, icon: "M",
                            label: mcp.method, color: .muted,
                            status: mcpStatus(mcp.method), mcpNamespace: mcp.namespace)
        }}
        if let known = tools[name] ?? toolsByLower[name.lowercased()] {{
            return Resolved(tier: known.tier, aggregate: known.aggregate, icon: known.icon,
                            label: known.label, color: known.color, status: known.status,
                            mcpNamespace: nil)
        }}
        let fallbackIcon = String(name.first.map {{ String($0).uppercased() }} ?? " ")
        return Resolved(tier: defaultTier, aggregate: defaultAggregate, icon: fallbackIcon,
                        label: name, color: .muted, status: "Using \\(name)", mcpNamespace: nil)
    }}

    private static func mcpStatus(_ method: String) -> String {{
        "Using " + method.replacingOccurrences(of: "_", with: " ")
    }}

    public static func tier(_ name: String) -> ToolTier {{
        resolve(name).tier
    }}

    public static func aggregate(_ name: String) -> ToolAggregate? {{
        resolve(name).aggregate
    }}

    /// Tools whose call input IS the agent's final answer (schema-constrained
    /// returns). Timeline builders render the payload as the closing assistant
    /// message instead of an opaque tool row.
    public static let finalAnswerTools: Set<String> = [{final_answer_tools_swift}]

    public static func isFinalAnswerTool(_ name: String) -> Bool {{
        let lower = name.lowercased()
        return finalAnswerTools.contains {{ $0.lowercased() == lower }}
    }}

    private static func parseMcp(_ name: String) -> (namespace: String, method: String)? {{
        let parts = name.components(separatedBy: "__")
        guard parts.count == 3, parts[0] == "mcp" else {{ return nil }}
        return (parts[1], parts[2])
    }}
}}

// --- Shell classifier constants (grammar is handwritten in ShellSalience.swift;
// parity with TS is enforced by config/shell-salience-fixtures.json). ---

public enum ShellClassifierConstants {{
    /// Lowercased; compare with `name.lowercased()`.
    public static let shellTools: Set<String> = [{", ".join(f'"{t.lower()}"' for t in shell["shell_tools"])}]
    public static let readOnlyCommands: Set<String> = [{", ".join(f'"{t}"' for t in shell["read_only_commands"])}]
    public static let gitReadSubcommands: Set<String> = [{", ".join(f'"{t}"' for t in shell["git_read_subcommands"])}]
    public static let aggregateByHead: [String: ToolAggregate] = [{", ".join(f'"{k}": .{v}' for k, v in shell["aggregate_by_head"].items())}]
    public static let defaultReadAggregate: ToolAggregate = .{shell["default_read_aggregate"]}
}}
"""


def main() -> int:
    data = load()
    outputs = {TS_OUT: render_ts(data), SWIFT_OUT: render_swift(data)}
    if "--check" in sys.argv[1:]:
        stale = [path for path, text in outputs.items() if not path.is_file() or path.read_text() != text]
        for path in stale:
            print(f"{path.relative_to(REPO)} is out of date; run python3 scripts/generate/tool_tiers.py")
        return 1 if stale else 0
    for path, text in outputs.items():
        path.write_text(text)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
