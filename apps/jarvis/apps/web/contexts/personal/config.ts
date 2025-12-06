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

Be conversational, helpful, and respect privacy. Use available tools to provide accurate, real-time information about the user's personal life and environment.

Keep responses concise but informative. When using tools, explain what you're checking and why it's relevant to the user's request.`,

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
