/**
 * Tool Factory
 * Creates and configures tools for the agent based on context
 *
 * v2.1 Architecture Note:
 * The route_to_supervisor tool has been REMOVED. In the one-brain architecture,
 * all user input (text and voice transcripts) goes directly to Supervisor via
 * SupervisorChatController → POST /api/jarvis/chat. There is no delegation tool.
 *
 * The remaining tools here are "direct tools" that can be called by Realtime for
 * quick operations (location, health data, notes search). In the future, these
 * should also move to Supervisor-owned tools (Phase 4 of v2.1 spec).
 */

import { tool } from '@openai/agents';
import { z } from 'zod';
import { CONFIG, toAbsoluteUrl } from './config';
import type { SessionManager } from '@jarvis/core';
import { stateManager } from './state-manager';

// ---------------------------------------------------------------------------
// Standard Tools (Direct Tools)
// These remain as Realtime tools for backward compatibility.
// Phase 4 of v2.1 spec will move these to Supervisor-owned tools.
// ---------------------------------------------------------------------------

function buildJsonHeaders(): HeadersInit {
  // Cookie-based auth - no Authorization header needed
  // Cookies are sent automatically with credentials: 'include' on fetch calls
  return { 'Content-Type': 'application/json' };
}

const locationTool = tool({
  name: 'get_current_location',
  description: 'Get current GPS location with coordinates and address. Call this whenever the user asks about their location.',
  parameters: z.object({}),
  async execute() {
    console.log('📍 Calling location tool');
    try {
      const response = await fetch(toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/tool`), {
        method: 'POST',
        headers: buildJsonHeaders(),
        credentials: 'include', // Cookie auth
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
  name: 'get_whoop_data',
  description: 'Get current WHOOP recovery score and health data',
  parameters: z.object({
    date: z.string().describe('Date in YYYY-MM-DD format, defaults to today').optional().nullable()
  }),
  async execute({ date }) {
    try {
      const response = await fetch(toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/tool`), {
        method: 'POST',
        headers: buildJsonHeaders(),
        credentials: 'include', // Cookie auth
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

const searchNotesTool = tool({
  name: 'search_notes',
  description: 'Search personal notes and knowledge base in Obsidian vault',
  parameters: z.object({
    query: z.string().describe('Search query for notes'),
    limit: z.number().optional().nullable().describe('Maximum number of results to return')
  }),
  async execute({ query, limit }) {
    console.log('📝 Calling search_notes tool:', query);
    try {
      const response = await fetch(toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/tool`), {
        method: 'POST',
        headers: buildJsonHeaders(),
        credentials: 'include', // Cookie auth
        body: JSON.stringify({
          name: 'obsidian.search_vault_smart',
          args: { query, limit: limit ?? 5 }
        })
      });

      if (!response.ok) throw new Error(`Notes search failed: ${response.status}`);

      const data = await response.json();

      // Detect echo fallback - means obsidian MCP is not configured
      if (data.echo) {
        console.warn('⚠️ Obsidian MCP not configured - received echo fallback');
        return 'Obsidian notes search is not configured. Please set up the Obsidian MCP server to enable this feature.';
      }

      if (data.error) return `Search error: ${data.error}`;
      if (!data.results || data.results.length === 0) {
        return `No notes found matching "${query}"`;
      }

      let result = `Found ${data.results.length} notes:\n`;
      for (const note of data.results) {
        result += `\n- ${note.title || note.path}`;
        if (note.excerpt) result += `\n  ${note.excerpt}`;
      }
      return result;
    } catch (error) {
      return `Failed to search notes: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  }
});

// ---------------------------------------------------------------------------
// Tool Factory Functions
// ---------------------------------------------------------------------------

function createMCPTool(toolConfig: any): any {
  if (toolConfig.name === 'get_current_location') {
    return locationTool;
  } else if (toolConfig.name === 'get_whoop_data') {
    return whoopTool;
  } else if (toolConfig.name === 'search_notes') {
    return searchNotesTool;
  }
  // Add more MCP tools mappings here
  console.warn(`Unknown MCP tool: ${toolConfig.name}`);
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
 * v2.1 Architecture: route_to_supervisor has been REMOVED.
 * All user input now goes directly to Supervisor via SupervisorChatController.
 * Only direct tools (location, whoop, notes) are registered with Realtime.
 */
export function createContextTools(config: any, sessionManager: SessionManager | null): any[] {
  const tools: any[] = [];

  // If bootstrap is available, treat it as the SSOT for enabled tools.
  // This prevents the client from registering tools the server considers disabled.
  const maybeGetBootstrap = (stateManager as any)?.getBootstrap;
  const bootstrap = typeof maybeGetBootstrap === 'function' ? maybeGetBootstrap.call(stateManager) : null;
  const enabledToolNames = bootstrap?.enabled_tools?.length
    ? new Set<string>(bootstrap.enabled_tools.map((t: any) => t?.name).filter(Boolean))
    : null;

  // v2.1: route_to_supervisor is REMOVED - Supervisor receives input directly via /api/jarvis/chat
  // No delegation tool needed since Realtime doesn't generate responses (create_response=false)

  // Add context-specific tools from config
  for (const toolConfig of config.tools) {
    if (!toolConfig.enabled) continue;

    // Skip route_to_supervisor if it somehow appears in config (v2.1 cleanup)
    if (toolConfig.name === 'route_to_supervisor') {
      console.log('🔧 Skipping route_to_supervisor (removed in v2.1)');
      continue;
    }

    if (enabledToolNames && !enabledToolNames.has(toolConfig.name)) {
      console.log(`🔧 Skipping ${toolConfig.name} (disabled by server)`);
      continue;
    }

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

  console.log(`🔧 Created ${tools.length} tools for Realtime session`);
  return tools;
}
