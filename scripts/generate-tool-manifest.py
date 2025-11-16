#!/usr/bin/env python3
"""Generate tool manifest for Jarvis and Zerg integration.

This script generates tool definitions for shared use between Jarvis and Zerg.
Based on common MCP tools available in the platform.

Output:
- packages/tool-manifest/index.ts - TypeScript tool definitions
- packages/tool-manifest/tools.py - Python tool definitions
"""

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).parent.parent


# Baseline tool definitions (will be extracted from backend in production)
BASELINE_TOOLS = [
    {
        "name": "whoop",
        "description": "WHOOP health and fitness data (recovery, sleep, strain)",
        "command": "uvx",
        "args": ["mcp-server-whoop"],
        "env": {},
        "contexts": ["personal"],
    },
    {
        "name": "obsidian",
        "description": "Obsidian vault note management",
        "command": "npx",
        "args": ["-y", "@rslangchain/mcp-obsidian"],
        "env": {},
        "contexts": ["personal"],
    },
    {
        "name": "traccar",
        "description": "GPS location tracking via Traccar",
        "command": "uvx",
        "args": ["mcp-traccar"],
        "env": {},
        "contexts": ["personal"],
    },
    {
        "name": "gmail",
        "description": "Gmail email management",
        "command": "npx",
        "args": ["-y", "gmail-mcp-server"],
        "env": {},
        "contexts": ["personal", "work"],
    },
    {
        "name": "slack",
        "description": "Slack workspace integration",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {},
        "contexts": ["personal", "work"],
    },
]


def extract_tool_definitions() -> List[Dict[str, Any]]:
    """Get tool definitions (baseline for now, extracted from backend later)."""
    return BASELINE_TOOLS


def generate_typescript(tools: List[Dict[str, Any]]) -> str:
    """Generate TypeScript tool manifest."""
    ts_code = '''/**
 * Tool Manifest - Auto-generated from Zerg MCP definitions
 * DO NOT EDIT MANUALLY - Run `npm run generate` in packages/tool-manifest
 */

export interface ToolDefinition {
  name: string;
  description: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  contexts: string[];
}

export const TOOL_MANIFEST: ToolDefinition[] = '''

    ts_code += json.dumps(tools, indent=2)
    ts_code += ";\n\n"

    ts_code += '''/**
 * Get tools available for a specific context
 */
export function getToolsForContext(context: string): ToolDefinition[] {
  return TOOL_MANIFEST.filter(tool => tool.contexts.includes(context));
}

/**
 * Get tool by name
 */
export function getToolByName(name: string): ToolDefinition | undefined {
  return TOOL_MANIFEST.find(tool => tool.name === name);
}
'''

    return ts_code


def generate_python(tools: List[Dict[str, Any]]) -> str:
    """Generate Python tool manifest."""
    py_code = '''"""Tool Manifest - Auto-generated from Zerg MCP definitions.

DO NOT EDIT MANUALLY - Run `npm run generate` in packages/tool-manifest
"""

from typing import Any

TOOL_MANIFEST: list[dict[str, Any]] = '''

    py_code += json.dumps(tools, indent=4)
    py_code += "\n\n\n"

    py_code += '''def get_tools_for_context(context: str) -> list[dict[str, Any]]:
    """Get tools available for a specific context."""
    return [tool for tool in TOOL_MANIFEST if context in tool["contexts"]]


def get_tool_by_name(name: str) -> dict[str, Any] | None:
    """Get tool by name."""
    for tool in TOOL_MANIFEST:
        if tool["name"] == name:
            return tool
    return None
'''

    return py_code


def validate_manifests() -> bool:
    """Validate that generated manifests match what's on disk."""
    print("🔍 Validating tool manifests are up-to-date...")

    tools = extract_tool_definitions()

    # Check TypeScript
    ts_output = REPO_ROOT / "packages" / "tool-manifest" / "index.ts"
    expected_ts = generate_typescript(tools)
    actual_ts = ts_output.read_text() if ts_output.exists() else ""

    # Check Python
    py_output = REPO_ROOT / "packages" / "tool-manifest" / "tools.py"
    expected_py = generate_python(tools)
    actual_py = py_output.read_text() if py_output.exists() else ""

    ts_matches = expected_ts == actual_ts
    py_matches = expected_py == actual_py

    if not ts_matches:
        print("❌ TypeScript manifest is out of sync")
        print("\nDiff:")
        diff = difflib.unified_diff(
            actual_ts.splitlines(keepends=True),
            expected_ts.splitlines(keepends=True),
            fromfile='current',
            tofile='expected'
        )
        print(''.join(diff))

    if not py_matches:
        print("❌ Python manifest is out of sync")
        print("\nDiff:")
        diff = difflib.unified_diff(
            actual_py.splitlines(keepends=True),
            expected_py.splitlines(keepends=True),
            fromfile='current',
            tofile='expected'
        )
        print(''.join(diff))

    if ts_matches and py_matches:
        print("✅ All tool manifests are up-to-date")
        return True
    else:
        print("\n💡 Run 'make generate-sdk' to regenerate manifests")
        return False


def main():
    """Generate or validate tool manifests."""
    parser = argparse.ArgumentParser(description='Generate or validate tool manifests')
    parser.add_argument('--validate', action='store_true',
                       help='Validate manifests without regenerating')
    args = parser.parse_args()

    if args.validate:
        if not validate_manifests():
            sys.exit(1)
        return

    # Original generation logic
    print("🔧 Generating tool manifest from Zerg MCP definitions...")

    tools = extract_tool_definitions()
    print(f"   Found {len(tools)} tools")

    # Generate TypeScript
    ts_output = REPO_ROOT / "packages" / "tool-manifest" / "index.ts"
    ts_code = generate_typescript(tools)
    ts_output.write_text(ts_code)
    print(f"   ✅ Generated TypeScript: {ts_output}")

    # Generate Python
    py_output = REPO_ROOT / "packages" / "tool-manifest" / "tools.py"
    py_code = generate_python(tools)
    py_output.write_text(py_code)
    print(f"   ✅ Generated Python: {py_output}")

    print("\n✨ Tool manifest generation complete!")
    print("\nNext steps:")
    print("  - Import in Jarvis contexts: import { TOOL_MANIFEST } from '@swarm/tool-manifest'")
    print("  - Import in Zerg backend: from swarm.tool_manifest import get_tools_for_context")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Error generating tool manifest: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
