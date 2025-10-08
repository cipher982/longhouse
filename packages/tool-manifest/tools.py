"""Tool Manifest - Auto-generated from Zerg MCP definitions.

DO NOT EDIT MANUALLY - Run `npm run generate` in packages/tool-manifest
"""

from typing import Any

TOOL_MANIFEST: list[dict[str, Any]] = [
    {
        "name": "whoop",
        "description": "WHOOP health and fitness data (recovery, sleep, strain)",
        "command": "uvx",
        "args": [
            "mcp-server-whoop"
        ],
        "env": {},
        "contexts": [
            "personal"
        ]
    },
    {
        "name": "obsidian",
        "description": "Obsidian vault note management",
        "command": "npx",
        "args": [
            "-y",
            "@rslangchain/mcp-obsidian"
        ],
        "env": {},
        "contexts": [
            "personal"
        ]
    },
    {
        "name": "traccar",
        "description": "GPS location tracking via Traccar",
        "command": "uvx",
        "args": [
            "mcp-traccar"
        ],
        "env": {},
        "contexts": [
            "personal"
        ]
    },
    {
        "name": "gmail",
        "description": "Gmail email management",
        "command": "npx",
        "args": [
            "-y",
            "gmail-mcp-server"
        ],
        "env": {},
        "contexts": [
            "personal",
            "work"
        ]
    },
    {
        "name": "slack",
        "description": "Slack workspace integration",
        "command": "npx",
        "args": [
            "-y",
            "@modelcontextprotocol/server-slack"
        ],
        "env": {},
        "contexts": [
            "personal",
            "work"
        ]
    }
]


def get_tools_for_context(context: str) -> list[dict[str, Any]]:
    """Get tools available for a specific context."""
    return [tool for tool in TOOL_MANIFEST if context in tool["contexts"]]


def get_tool_by_name(name: str) -> dict[str, Any] | None:
    """Get tool by name."""
    for tool in TOOL_MANIFEST:
        if tool["name"] == name:
            return tool
    return None
