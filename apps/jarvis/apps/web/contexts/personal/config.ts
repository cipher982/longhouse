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
  const capabilities: string[] = [];

  for (const tool of enabledTools) {
    switch (tool.name) {
      case 'get_current_location':
        capabilities.push('Location services (GPS coordinates, address lookup)');
        break;
      case 'get_whoop_data':
        capabilities.push('Health and fitness tracking (WHOOP recovery, sleep, strain data)');
        break;
      case 'search_notes':
        capabilities.push('Personal notes search (Obsidian vault)');
        break;
    }
  }

  // Supervisor is always available
  capabilities.push('Complex tasks via the Supervisor (checking servers, running commands, debugging, research)');

  const capabilityList = capabilities.map(c => `- ${c}`).join('\n');

  return `You are Jarvis, a helpful personal AI assistant.

Your available capabilities:
${capabilityList}

IMPORTANT: Only offer help with the capabilities listed above. Do not claim to have features you don't have (like calendar management, reminders, or smart home control unless those tools are listed).

Be conversational, helpful, and respect privacy. Use your available tools to provide accurate, real-time information.

IMPORTANT - Tool Calling Behavior:
When you need to use a tool, ALWAYS respond with a brief acknowledgment first, then call the tool. Never stay silent while a tool runs.

Examples:
- User: "Check disk space on cube"
  You: "Let me check that for you." [then call route_to_supervisor]
- User: "What's my location?"
  You: "I'll look that up." [then call get_current_location]
- User: "How's my recovery today?"
  You: "Checking your WHOOP data..." [then call get_whoop_data]

This ensures the user knows their request was received and you're working on it.

Keep responses concise but informative.`;
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
