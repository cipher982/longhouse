/**
 * Work context configuration template for Zeta
 * Copy this to config.ts and customize for your company
 * 
 * IMPORTANT: This template is committed, but config.ts is gitignored
 */

import type { VoiceAgentConfig } from '../types';

export const zetaConfig: VoiceAgentConfig = {
  name: 'Zeta',
  description: 'AI assistant for work with company knowledge and business tools',
  
  instructions: `You are Zeta, a professional AI assistant for work contexts. You have access to:

- Company knowledge base and documentation
- Financial data and business metrics  
- Product information and specifications
- Team information and organizational data
- Strategic planning and policy documents

Focus on providing accurate, business-relevant information. Use company knowledge search tools to find current information before answering questions about company matters.

Maintain a professional tone while being helpful and efficient. Prioritize company data and established policies in your responses.`,

  theme: {
    primaryColor: '#7c3aed',      // Purple-600 (company brand)
    secondaryColor: '#475569',    // Slate-600
    backgroundColor: '#1e1b4b',   // Indigo-900 (professional dark)
    textColor: '#f1f5f9',        // Slate-100
    accentColor: '#8b5cf6',      // Purple-500
    borderColor: '#334155'       // Slate-700
  },

  branding: {
    title: 'Zeta',
    subtitle: 'Work Assistant',
    logoUrl: '/assets/company-logo.png',  // Add your company logo
    favicon: '/assets/company-favicon.ico'
  },

  tools: [
    {
      name: 'search_company_knowledge',
      description: 'Search company documentation, policies, and business data',
      enabled: true,
      ragDatabase: 'company_knowledge',
      ragCollection: 'documents'
    },
    {
      name: 'get_financial_data',
      description: 'Access financial reports and business metrics',
      enabled: true,
      ragDatabase: 'company_knowledge', 
      ragCollection: 'financial'
    },
    {
      name: 'search_team_info',
      description: 'Find team member information and organizational data',
      enabled: true,
      ragDatabase: 'company_knowledge',
      ragCollection: 'organizational'
    }
    // Add your company-specific tools here
  ],

  apiEndpoints: {
    tokenMinting: '/session',
    toolExecution: '/tool'  // Or company-specific endpoint
  },

  settings: {
    maxHistoryTurns: 100,    // Longer context for business conversations
    enableRAG: true,         // Core feature for work context
    enableMCP: false,        // Work context uses RAG, not personal MCP
    voiceModel: 'gpt-realtime',
    defaultPrompts: [
      "What were our Q3 results?",
      "Show me the latest product roadmap",
      "What's our remote work policy?", 
      "Who's on the engineering team?"
    ]
  }
};