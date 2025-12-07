/**
 * Personal context configuration for Jarvis
 * This represents your personal AI assistant setup
 */

import type { VoiceAgentConfig } from '../types';

export const personalConfig: VoiceAgentConfig = {
  name: 'Jarvis',
  description: 'Your personal AI assistant with access to health data, location, notes, and smart home',

  instructions: `You are Jarvis, a helpful personal AI assistant. You have access to personal data and can help with:

- Health and fitness tracking (WHOOP data, workout analysis)
- Location services (GPS coordinates, travel assistance)
- Personal productivity (calendar, notes, reminders)
- Smart home control and automation
- Personal knowledge base and memory
- Complex tasks via the Supervisor (checking servers, debugging, research)

Be conversational, helpful, and respect privacy. Use available tools to provide accurate, real-time information about the user's personal life and environment.

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

Keep responses concise but informative. When using tools, briefly acknowledge the request before calling the tool.`,

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

  tools: [
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
  ],

  apiEndpoints: {
    tokenMinting: '/session',
    toolExecution: '/tool'
  },

  sync: {
    baseUrl: import.meta.env?.VITE_SYNC_BASE_URL || ''
  },

  settings: {
    maxHistoryTurns: 50,
    enableRAG: false,        // Personal context uses MCP, not RAG
    enableMCP: true,         // Core feature for personal assistant
    voiceModel: 'gpt-realtime',
    defaultPrompts: [
      "What's my current location?",
      "How's my recovery today?",
      "Show me my recent notes",
      "What should I focus on today?"
    ]
  }
};
