#!/usr/bin/env python3
"""Generate TypeScript and Swift tool-tier modules from config/tool-tiers.json.

Single source of truth: config/tool-tiers.json.
Outputs:
  - web/src/lib/sessionWorkspace/toolTiers.generated.ts
  - ios/Sources/Shared/ToolTiers.generated.swift
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config" / "tool-tiers.json"
TS_OUT = REPO / "web" / "src" / "lib" / "sessionWorkspace" / "toolTiers.generated.ts"
SWIFT_OUT = REPO / "ios" / "Sources" / "Shared" / "ToolTiers.generated.swift"


def load() -> dict:
    return json.loads(CONFIG.read_text())


def render_ts(data: dict) -> str:
    tools = data["tools"]
    mcp_ns = data["mcp_namespaces"]
    default_tier = data["default_tier"]
    mcp_default_tier = data["mcp_default_tier"]

    def quote(v: str) -> str:
        return json.dumps(v)

    entries = []
    for name, meta in tools.items():
        entries.append(
            f'  {quote(name)}: {{ tier: {quote(meta["tier"])}, icon: {quote(meta["icon"])}, '
            f'label: {quote(meta["label"])}, color: {quote(meta["color"])} }},'
        )

    mcp_entries = []
    for ns, meta in mcp_ns.items():
        mcp_entries.append(
            f'  {quote(ns)}: {{ icon: {quote(meta["icon"])}, color: {quote(meta["color"])} }},'
        )

    return f"""// @generated from config/tool-tiers.json — do not edit by hand.
// Run: python3 scripts/generate/tool_tiers.py

export type ToolTier = "noise" | "context" | "action";
export type ToolColorToken =
  | "brand" | "cyan" | "success" | "warning" | "secondary"
  | "tertiary" | "accent" | "muted";

export interface ToolTierMeta {{
  tier: ToolTier;
  icon: string;
  label: string;
  color: ToolColorToken;
}}

export interface McpNamespaceMeta {{
  icon: string;
  color: ToolColorToken;
}}

export const DEFAULT_TOOL_TIER: ToolTier = {quote(default_tier)};
export const MCP_DEFAULT_TIER: ToolTier = {quote(mcp_default_tier)};

export const TOOL_TIERS: Record<string, ToolTierMeta> = {{
{chr(10).join(entries)}
}};

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
  icon: string;
  label: string;
  color: ToolColorToken;
  mcpNamespace?: string;
}}

export function resolveToolInfo(toolName: string): ResolvedToolInfo {{
  const mcp = parseMcp(toolName);
  if (mcp) {{
    const ns = mcp.namespace.toLowerCase();
    for (const [prefix, meta] of Object.entries(MCP_NAMESPACES)) {{
      if (ns.includes(prefix)) {{
        return {{
          tier: MCP_DEFAULT_TIER,
          icon: meta.icon,
          label: mcp.method,
          color: meta.color,
          mcpNamespace: mcp.namespace,
        }};
      }}
    }}
    return {{
      tier: MCP_DEFAULT_TIER,
      icon: "M",
      label: mcp.method,
      color: "muted",
      mcpNamespace: mcp.namespace,
    }};
  }}

  const exact = TOOL_TIERS[toolName];
  if (exact) return {{ ...exact }};

  const lower = toolName.toLowerCase();
  for (const [key, meta] of Object.entries(TOOL_TIERS)) {{
    if (key.toLowerCase() === lower) return {{ ...meta }};
  }}

  return {{
    tier: DEFAULT_TOOL_TIER,
    icon: (toolName[0] || " ").toUpperCase(),
    label: toolName,
    color: "muted",
  }};
}}

export function toolTier(toolName: string): ToolTier {{
  return resolveToolInfo(toolName).tier;
}}
"""


def render_swift(data: dict) -> str:
    tools = data["tools"]
    mcp_ns = data["mcp_namespaces"]

    tool_entries = []
    for name, meta in tools.items():
        tool_entries.append(
            f'        "{name}": ToolTierMeta(tier: .{meta["tier"]}, '
            f'icon: "{meta["icon"]}", label: "{meta["label"]}", color: .{meta["color"]}),'
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

public enum ToolColorToken: String, Sendable {{
    case brand, cyan, success, warning, secondary, tertiary, accent, muted
}}

public struct ToolTierMeta: Sendable {{
    public let tier: ToolTier
    public let icon: String
    public let label: String
    public let color: ToolColorToken
}}

public struct McpNamespaceMeta: Sendable {{
    public let icon: String
    public let color: ToolColorToken
}}

public enum ToolTiers {{
    public static let defaultTier: ToolTier = .{data["default_tier"]}
    public static let mcpDefaultTier: ToolTier = .{data["mcp_default_tier"]}

    public static let tools: [String: ToolTierMeta] = [
{chr(10).join(tool_entries)}
    ]

    public static let mcpNamespaces: [String: McpNamespaceMeta] = [
{chr(10).join(mcp_entries)}
    ]

    public struct Resolved: Sendable {{
        public let tier: ToolTier
        public let icon: String
        public let label: String
        public let color: ToolColorToken
        public let mcpNamespace: String?
    }}

    public static func resolve(_ name: String) -> Resolved {{
        if let mcp = parseMcp(name) {{
            let ns = mcp.namespace.lowercased()
            for (prefix, meta) in mcpNamespaces where ns.contains(prefix) {{
                return Resolved(tier: mcpDefaultTier, icon: meta.icon,
                                label: mcp.method, color: meta.color,
                                mcpNamespace: mcp.namespace)
            }}
            return Resolved(tier: mcpDefaultTier, icon: "M",
                            label: mcp.method, color: .muted,
                            mcpNamespace: mcp.namespace)
        }}
        if let exact = tools[name] {{
            return Resolved(tier: exact.tier, icon: exact.icon, label: exact.label,
                            color: exact.color, mcpNamespace: nil)
        }}
        let lower = name.lowercased()
        for (key, meta) in tools where key.lowercased() == lower {{
            return Resolved(tier: meta.tier, icon: meta.icon, label: meta.label,
                            color: meta.color, mcpNamespace: nil)
        }}
        let fallbackIcon = String(name.first.map {{ String($0).uppercased() }} ?? " ")
        return Resolved(tier: defaultTier, icon: fallbackIcon, label: name,
                        color: .muted, mcpNamespace: nil)
    }}

    public static func tier(_ name: String) -> ToolTier {{
        resolve(name).tier
    }}

    private static func parseMcp(_ name: String) -> (namespace: String, method: String)? {{
        let parts = name.components(separatedBy: "__")
        guard parts.count == 3, parts[0] == "mcp" else {{ return nil }}
        return (parts[1], parts[2])
    }}
}}
"""


def main() -> int:
    data = load()
    TS_OUT.write_text(render_ts(data))
    SWIFT_OUT.write_text(render_swift(data))
    print(f"wrote {TS_OUT}")
    print(f"wrote {SWIFT_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
