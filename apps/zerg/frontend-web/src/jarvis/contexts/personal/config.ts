/**
 * Personal context configuration for Jarvis
 * This represents your personal AI assistant setup
 */

import type { VoiceAgentConfig, ToolConfig } from '../types';
import { getRealtimeModel } from '../../core';
import { stateManager } from '../../lib/state-manager';

/**
 * Generate dynamic instructions based on which tools are actually enabled.
 * This prevents the AI from claiming capabilities it doesn't have.
 *
 * NOTE: This is a fallback for when server bootstrap fails. The server-provided
 * prompt from /api/jarvis/bootstrap is preferred as it includes user context.
 */
function generateInstructions(tools: ToolConfig[]): string {
  const enabledTools = tools.filter(t => t.enabled);

  // Build a capability list for informational display only.
  const maybeCapabilities: string[] = [];

  for (const tool of enabledTools) {
    switch (tool.name) {
      case 'get_current_location':
        maybeCapabilities.push('**Location** - Get current GPS coordinates and address');
        break;
      case 'get_whoop_data':
        maybeCapabilities.push('**Health metrics** - WHOOP recovery score, sleep quality, strain data');
        break;
      case 'search_notes':
        maybeCapabilities.push('**Notes search** - Query notes and knowledge base');
        break;
    }
  }

  const capabilityList = maybeCapabilities.length > 0
    ? maybeCapabilities.map(c => `  - ${c}`).join('\n')
    : '  (No tools currently enabled)';

  return `You are Concierge. You provide voice I/O (transcription + turn-taking cues).

## Architecture (v2.1 One-Brain)

- Concierge (server) is the ONLY brain and generates all assistant responses.
- This Realtime session is I/O ONLY. Do NOT generate assistant responses.
- Do NOT call tools from this session.

## Tool Awareness (informational)

These capabilities may exist server-side (via Concierge tools) depending on configuration:
${capabilityList}

If the user asks for something that requires tools, respond that the request will be handled by Concierge.`;
}

/**
 * Get instructions - prefers server-provided prompt from bootstrap,
 * falls back to client-side generation if bootstrap unavailable.
 */
function getInstructions(): string {
  const bootstrap = stateManager.getBootstrap();
  if (bootstrap?.prompt) {
    return bootstrap.prompt;
  }
  // Fallback to client-side generation
  return generateInstructions(toolsConfig);
}

// Tool definitions for informational display only (v2.1 Phase 4).
// Actual tool execution happens server-side via Supervisor.
// These are listed here so fallback instructions can describe capabilities.
const toolsConfig: ToolConfig[] = [
  {
    name: 'get_current_location',
    description: 'Get current GPS location with coordinates and address',
    enabled: true,
    // Executed by Concierge via Traccar connector
  },
  {
    name: 'get_whoop_data',
    description: 'Get WHOOP health metrics (recovery, sleep, strain)',
    enabled: true,
    // Executed by Concierge via WHOOP connector
  },
  {
    name: 'search_notes',
    description: 'Search personal notes and knowledge base',
    enabled: true,
    // Executed by Concierge via Runner (Obsidian vault)
  }
];

export const personalConfig: VoiceAgentConfig = {
  name: 'Concierge',
  description: 'Your personal AI assistant',

  // Use getter to allow dynamic lookup of server-provided prompt
  get instructions() {
    return getInstructions();
  },

  theme: {
    primaryColor: '#0891b2',      // Cyan-600
    secondaryColor: '#334155',    // Slate-700
    backgroundColor: '#0b1220',   // Dark blue-gray
    textColor: '#e5e7eb',        // Gray-200
    accentColor: '#06b6d4',      // Cyan-500
    borderColor: '#1f2937'       // Gray-800
  },

  branding: {
    title: 'Concierge',
    subtitle: 'Personal AI Assistant',
    favicon: '/icon-192.png'
  },

  tools: toolsConfig,

  apiEndpoints: {
    tokenMinting: '/session',
    // toolExecution removed in v2.1 - tools execute via Concierge
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
