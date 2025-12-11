/**
 * Personal context configuration for Jarvis
 * This represents your personal AI assistant setup
 */

import type { VoiceAgentConfig, ToolConfig } from '../types';
import { getRealtimeModel } from '@jarvis/core';

/**
 * Generate dynamic instructions based on which tools are actually enabled.
 * This prevents the AI from claiming capabilities it doesn't have.
 */
function generateInstructions(tools: ToolConfig[]): string {
  const enabledTools = tools.filter(t => t.enabled);

  // Build capability list from actual tools
  const directCapabilities: string[] = [];

  for (const tool of enabledTools) {
    switch (tool.name) {
      case 'get_current_location':
        directCapabilities.push('**Location** - Get current GPS coordinates and address via Traccar');
        break;
      case 'get_whoop_data':
        directCapabilities.push('**Health metrics** - WHOOP recovery score, sleep quality, strain data');
        break;
      case 'search_notes':
        directCapabilities.push('**Notes search** - Query the Obsidian vault for past notes and knowledge');
        break;
    }
  }

  const directCapabilityList = directCapabilities.length > 0
    ? directCapabilities.map(c => `  - ${c}`).join('\n')
    : '  (No direct tools currently enabled)';

  return `You are Jarvis, a personal AI assistant. You're conversational, concise, and actually useful.

## Who You Serve
You serve your user - help them get things done efficiently. They may have servers to manage, health data to track, and notes to search.

## Your Architecture
You have two modes of operation:

**1. Direct Tools (instant, < 2 seconds)**
These you can call immediately:
${directCapabilityList}

**2. Supervisor Delegation (5-60 seconds)**
For anything requiring server access, investigation, or multi-step work, use \`route_to_supervisor\`. The Supervisor has workers that can:
  - SSH into the user's configured servers
  - Check disk space, docker containers, logs, backups
  - Run shell commands and analyze output
  - Investigate issues and report findings

## When to Delegate vs Answer Directly

**Use route_to_supervisor for:**
- Checking servers, disk space, containers, logs
- "Are my backups working?" → needs to run commands
- "Why is X slow?" → needs investigation
- Anything mentioning servers, docker, debugging

**Answer directly for:**
- Direct tool queries (location, health data, notes)
- General knowledge, conversation, jokes
- Time, date, simple facts

## Response Style

**Be conversational and concise.**

**When using tools:**
1. Say a brief acknowledgment FIRST ("Let me check that", "Looking that up")
2. THEN call the tool
3. Never go silent while a tool runs

## What You DON'T Have
Be honest about limitations. You cannot:
- Manage calendars or reminders (no tool for this)
- Control smart home devices (no tool for this)

If asked about something you can't do, say so clearly.`;
}

// Define tools first so we can use them in instruction generation
const toolsConfig: ToolConfig[] = [
  {
    name: 'get_current_location',
    description: 'Get current GPS location with coordinates and address',
    enabled: true,
    mcpServer: 'traccar-mcp',
    mcpFunction: 'location.get_current'
  },
  {
    name: 'get_whoop_data',
    description: 'Get WHOOP health metrics (recovery, sleep, strain)',
    enabled: true,
    mcpServer: 'whoop-mcp',
    mcpFunction: 'whoop.get_health_status'
  },
  {
    name: 'search_notes',
    description: 'Search personal notes and knowledge base',
    enabled: true,
    mcpServer: 'obsidian-mcp',
    mcpFunction: 'obsidian.search_vault_smart'
  }
];

export const personalConfig: VoiceAgentConfig = {
  name: 'Jarvis',
  description: 'Your personal AI assistant',

  instructions: generateInstructions(toolsConfig),

  theme: {
    primaryColor: '#0891b2',      // Cyan-600
    secondaryColor: '#334155',    // Slate-700
    backgroundColor: '#0b1220',   // Dark blue-gray
    textColor: '#e5e7eb',        // Gray-200
    accentColor: '#06b6d4',      // Cyan-500
    borderColor: '#1f2937'       // Gray-800
  },

  branding: {
    title: 'Jarvis',
    subtitle: 'Personal AI Assistant',
    favicon: '/icon-192.png'
  },

  tools: toolsConfig,

  apiEndpoints: {
    tokenMinting: '/session',
    toolExecution: '/tool'
  },

  sync: {
    baseUrl: import.meta.env?.VITE_SYNC_BASE_URL || ''
  },

  settings: {
    maxHistoryTurns: 50,
    realtimeHistoryTurns: 8, // Turns to inject into OpenAI Realtime session
    enableRAG: false,        // Personal context uses MCP, not RAG
    enableMCP: true,         // Core feature for personal assistant
    voiceModel: getRealtimeModel(),
    defaultPrompts: [
      "What's my current location?",
      "How's my recovery today?",
      "Show me my recent notes",
      "What should I focus on today?"
    ]
  }
};
