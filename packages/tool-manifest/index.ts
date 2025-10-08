/**
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

export const TOOL_MANIFEST: ToolDefinition[] = [
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
];

/**
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
