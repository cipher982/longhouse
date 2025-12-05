/**
 * Tool Factory
 * Creates and configures tools for the agent based on context
 */

import { tool } from '@openai/agents';
import { z } from 'zod';
import { CONFIG } from './config';
import type { SessionManager, SupervisorEvent } from '@jarvis/core';
import { stateManager } from './state-manager';

// ---------------------------------------------------------------------------
// Standard Tools
// ---------------------------------------------------------------------------

const locationTool = tool({
  name: 'get_current_location',
  description: 'Get current GPS location with coordinates and address. Call this whenever the user asks about their location.',
  parameters: z.object({}),
  async execute() {
    console.log('📍 Calling location tool');
    try {
      const response = await fetch(`${CONFIG.API_BASE}/tool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: 'location.get_current',
          args: { include_address: true }
        })
      });

      if (!response.ok) throw new Error(`Location API failed: ${response.status}`);

      const data = await response.json();
      if (data.error) return `Location error: ${data.error}`;

      const loc = Array.isArray(data) ? data[0] : data;
      if (!loc) return "No location data available";

      let result = `Current location: ${loc.lat?.toFixed(4)}, ${loc.lon?.toFixed(4)}`;
      if (loc.address) result += ` (${loc.address})`;
      return result;
    } catch (error) {
      return `Failed to get location: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  }
});

const whoopTool = tool({
  name: 'get_whoop_recovery',
  description: 'Get current WHOOP recovery score and health data',
  parameters: z.object({
    date: z.string().describe('Date in YYYY-MM-DD format, defaults to today').optional().nullable()
  }),
  async execute({ date }) {
    try {
      const response = await fetch(`${CONFIG.API_BASE}/tool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: 'whoop.get_daily',
          args: { date }
        })
      });

      if (!response.ok) throw new Error(`WHOOP API failed: ${response.status}`);

      const data = await response.json();
      let result = 'Your WHOOP data:\n';
      if (data.recovery_score) result += `Recovery Score: ${data.recovery_score}%\n`;
      if (data.strain) result += `Strain: ${data.strain}\n`;
      if (data.sleep_duration) result += `Sleep: ${data.sleep_duration} hours\n`;
      return result;
    } catch (error) {
      return `Sorry, couldn't get your WHOOP data: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  }
});

// ---------------------------------------------------------------------------
// Supervisor Tool - Routes complex tasks to Zerg Supervisor
// ---------------------------------------------------------------------------

/**
 * Route to Supervisor Tool
 *
 * This tool allows OpenAI Realtime to delegate complex tasks to the Zerg Supervisor.
 * The supervisor can spawn workers to investigate, research, and execute multi-step tasks.
 *
 * Intent Detection Keywords (from Super Siri architecture):
 * - Supervisor triggers: "check", "investigate", "research", "why", "debug", "analyze"
 * - Quick mode: "what", "when", "tell me", simple facts
 */
export const routeToSupervisorTool = tool({
  name: 'route_to_supervisor',
  description: `Delegate a complex task to the Zerg Supervisor for investigation. Use this for tasks that require:
- Checking servers, services, or infrastructure health
- Running commands on remote systems
- Multi-step investigations or debugging
- Research that requires external data gathering
- Any task that might take more than a few seconds
- Tasks with keywords like: check, investigate, research, why, debug, analyze

Do NOT use this for simple questions like "what time is it" or "tell me a joke".

The supervisor will spawn workers as needed and synthesize a final answer.`,
  parameters: z.object({
    task: z.string().describe('The task to delegate to the supervisor (natural language description of what needs to be done)'),
    reason: z.string().optional().nullable().describe('Brief reason why this needs supervisor delegation (for logging/debugging)')
  }),
  async execute({ task, reason }) {
    console.log(`🎯 route_to_supervisor called: "${task}" (reason: ${reason || 'none'})`);

    try {
      const jarvisClient = stateManager.getJarvisClient();

      if (!jarvisClient) {
        console.error('❌ JarvisClient not available');
        return 'Sorry, I cannot connect to the backend right now. Please try again later.';
      }

      if (!jarvisClient.isAuthenticated()) {
        console.error('❌ JarvisClient not authenticated');
        return 'Sorry, I am not authenticated with the backend. Please reconnect and try again.';
      }

      console.log('🚀 Dispatching task to supervisor...');

      // Execute the supervisor task and wait for completion
      // The executeSupervisorTask method handles SSE subscription internally
      const result = await jarvisClient.executeSupervisorTask(task, {
        timeout: 120000, // 2 minute timeout for complex tasks
        onProgress: (event: SupervisorEvent) => {
          // Log progress events for debugging
          console.log(`📡 Supervisor progress: ${event.type}`, event.payload);
        }
      });

      console.log('✅ Supervisor task completed');
      return result;

    } catch (error) {
      console.error('❌ Supervisor task failed:', error);
      const errorMsg = error instanceof Error ? error.message : 'Unknown error';

      // Provide user-friendly error messages
      if (errorMsg.includes('timeout')) {
        return 'The investigation took too long. Please try a more specific request.';
      } else if (errorMsg.includes('Not authenticated')) {
        return 'I lost connection to the backend. Please reconnect and try again.';
      } else if (errorMsg.includes('SSE stream error')) {
        return 'Lost connection while processing. Please try again.';
      } else {
        return `I encountered an issue while investigating: ${errorMsg}`;
      }
    }
  }
});

/**
 * Create the route_to_supervisor tool
 *
 * This is called by createContextTools when supervisor delegation is enabled
 */
export function createSupervisorTool(): typeof routeToSupervisorTool {
  return routeToSupervisorTool;
}

// ---------------------------------------------------------------------------
// Tool Factory Functions
// ---------------------------------------------------------------------------

function createMCPTool(toolConfig: any): any {
  if (toolConfig.name === 'get_current_location') {
    return locationTool;
  } else if (toolConfig.name === 'get_whoop_data') {
    return whoopTool;
  }
  // Add more MCP tools mappings here
  return null;
}

function createRAGTool(toolConfig: any, sessionManager: SessionManager | null): any {
  const baseExecute = async ({ query, category }: { query: string, category?: string }) => {
    console.log(`🔍 ${toolConfig.name}:`, query, category);
    try {
      if (!sessionManager) {
        return 'RAG search not available - session not initialized';
      }

      const searchOptions: any = { limit: 3 };
      if (category && category !== 'any') {
        searchOptions.type = category as 'financial' | 'product' | 'policy' | 'organizational' | 'strategic';
      }

      const results = await sessionManager.searchDocuments(query, searchOptions);

      if (results.length === 0) {
        return `No company information found for "${query}"`;
      }

      let response = `Found ${results.length} relevant company documents:\n\n`;
      results.forEach((result: any, i: number) => {
        const doc = result.document;
        response += `${i + 1}. **${doc.metadata.type.toUpperCase()}** (relevance: ${(result.score * 100).toFixed(1)}%)\n`;
        response += `   ${doc.content}\n`;
        response += `   Source: ${doc.metadata.source}\n\n`;
      });

      return response;
    } catch (error) {
      console.error(`${toolConfig.name} failed:`, error);
      return `Search failed: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  };

  // Create tool based on config name
  if (toolConfig.name === 'search_company_knowledge') {
    return tool({
      name: 'search_company_knowledge',
      description: 'Search company documentation, policies, and business data',
      parameters: z.object({
        query: z.string().describe('Search query for company information'),
        category: z.string().describe('Category to filter by: any, financial, product, policy, organizational, strategic').default('any')
      }),
      execute: baseExecute
    });
  } else if (toolConfig.name === 'get_financial_data') {
    return tool({
      name: 'get_financial_data',
      description: 'Access financial reports and business metrics',
      parameters: z.object({
        query: z.string().describe('Query for financial data (revenue, profits, Q3 results, etc.)'),
        category: z.string().describe('Category: any, financial').default('financial')
      }),
      execute: baseExecute
    });
  } else if (toolConfig.name === 'search_team_info') {
    return tool({
      name: 'search_team_info',
      description: 'Find team member information and organizational data',
      parameters: z.object({
        query: z.string().describe('Query for team/organizational info'),
        category: z.string().describe('Category: any, organizational').default('organizational')
      }),
      execute: baseExecute
    });
  }

  console.warn(`Unknown RAG tool: ${toolConfig.name}`);
  return null;
}

// ---------------------------------------------------------------------------
// Main Export: Create all tools for a context
// ---------------------------------------------------------------------------

/**
 * Create all tools for a given context configuration
 *
 * Always includes the route_to_supervisor tool for complex task delegation.
 * Additional tools are created based on the context configuration.
 */
export function createContextTools(config: any, sessionManager: SessionManager | null): any[] {
  const tools: any[] = [];

  // Always include the supervisor tool for complex task delegation
  // This enables Jarvis to route complex requests to the Zerg Supervisor
  tools.push(routeToSupervisorTool);
  console.log('🔧 Added route_to_supervisor tool');

  // Add context-specific tools from config
  for (const toolConfig of config.tools) {
    if (!toolConfig.enabled) continue;

    let t = null;
    if (toolConfig.mcpServer && toolConfig.mcpFunction) {
      t = createMCPTool(toolConfig);
    } else if (toolConfig.ragDatabase && toolConfig.ragCollection) {
      t = createRAGTool(toolConfig, sessionManager);
    }

    if (t) {
      tools.push(t);
    }
  }

  console.log(`🔧 Created ${tools.length} tools total`);
  return tools;
}
