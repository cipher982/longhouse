/**
 * JarvisChatPage - Entry point for the Jarvis chat UI within the Zerg SPA
 *
 * This wraps the Jarvis React app (formerly standalone) in its context provider
 * and loads the Jarvis-specific CSS styles.
 *
 * Supports URL params:
 * - ?thread=<title> - Load a backend thread by title for display
 */

import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

// Import Jarvis styles (now loaded globally via styles/app.css)

// Import Jarvis app and context
import { AppProvider, type ChatMessage, type StoredToolCall } from '../jarvis/app/context';
import App from '../jarvis/app/App';

// Import tool store for hydration
import { supervisorToolStore, type SupervisorToolCall } from '../jarvis/lib/supervisor-tool-store';

// API functions
import { fetchThreadByTitle, fetchThreadMessages } from '../services/api';

export default function JarvisChatPage() {
  const [searchParams] = useSearchParams();
  const threadTitle = searchParams.get('thread');
  const [initialMessages, setInitialMessages] = useState<ChatMessage[] | undefined>(undefined);
  const [loading, setLoading] = useState(!!threadTitle);

  useEffect(() => {
    if (!threadTitle) return;

    async function loadThreadMessages() {
      try {
        const thread = await fetchThreadByTitle(threadTitle!);
        if (!thread) {
          console.warn(`Thread "${threadTitle}" not found`);
          setLoading(false);
          return;
        }

        const messages = await fetchThreadMessages(thread.id);

        // Clear any existing tools from previous loads
        supervisorToolStore.clearTools();

        // Collect tools to hydrate into the store
        const toolsToLoad: SupervisorToolCall[] = [];

        // Convert backend ThreadMessages to Jarvis ChatMessages
        // Filter to only user/assistant roles (skip system and tool messages)
        const chatMessages: ChatMessage[] = messages
          .filter((m) => m.role === 'user' || m.role === 'assistant')
          .map((m) => {
            // Extract tool_calls if present (assistant messages with tool calls)
            // The API returns tool_calls in LangChain format
            const apiToolCalls = (m as { tool_calls?: Array<{ id: string; name: string; args: Record<string, unknown> }> }).tool_calls;
            const toolCalls: StoredToolCall[] | undefined = apiToolCalls && apiToolCalls.length > 0
              ? apiToolCalls.map(tc => ({
                  id: tc.id,
                  name: tc.name,
                  args: tc.args || {},
                }))
              : undefined;

            // Generate synthetic runId for messages with tool calls
            // Use negative message ID to avoid collision with real run IDs
            const syntheticRunId = toolCalls ? -m.id : undefined;

            // Convert tool_calls to SupervisorToolCall format for the store
            if (toolCalls && syntheticRunId !== undefined) {
              for (const tc of toolCalls) {
                toolsToLoad.push({
                  toolCallId: tc.id,
                  toolName: tc.name,
                  status: 'completed', // Historical tools are always completed
                  runId: syntheticRunId,
                  startedAt: m.sent_at ? new Date(m.sent_at).getTime() : Date.now(),
                  completedAt: m.sent_at ? new Date(m.sent_at).getTime() : Date.now(),
                  argsPreview: JSON.stringify(tc.args).slice(0, 100),
                  args: tc.args,
                  logs: [],
                });
              }
            }

            return {
              id: String(m.id),
              role: m.role as 'user' | 'assistant',
              content: m.content || '',
              timestamp: m.sent_at ? new Date(m.sent_at) : new Date(),
              skipAnimation: true, // Don't animate pre-loaded messages
              runId: syntheticRunId,
              toolCalls,
            };
          });

        // Hydrate the tool store with historical tools
        if (toolsToLoad.length > 0) {
          supervisorToolStore.loadTools(toolsToLoad);
        }

        setInitialMessages(chatMessages);
      } catch (error) {
        console.error('Failed to load thread:', error);
      } finally {
        setLoading(false);
      }
    }

    loadThreadMessages();
  }, [threadTitle]);

  // Show loading state while fetching thread
  if (loading) {
    return (
      <div className="jarvis-container jarvis-loading">
        <div className="jarvis-loading-text">Loading conversation...</div>
      </div>
    );
  }

  return (
    <AppProvider initialMessages={initialMessages}>
      <div className="jarvis-container">
        <App embedded={true} />
      </div>
    </AppProvider>
  );
}
