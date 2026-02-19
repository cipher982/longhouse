/**
 * OikosChatPage - Entry point for the Oikos chat UI within the Zerg SPA
 *
 * This wraps the Oikos React app (formerly standalone) in its context provider
 * and loads the Oikos-specific CSS styles.
 *
 * Supports URL params:
 * - ?thread=<title> - Load a backend thread by title for display
 */

import { useEffect, useState, useCallback } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useSessionPicker } from '../components/SessionPickerProvider';
import { eventBus } from '../oikos/lib/event-bus';

// Import Oikos styles (now loaded globally via styles/app.css)

// Import Oikos app and context
import { AppProvider, type ChatMessage, type StoredToolCall } from '../oikos/app/context';
import App from '../oikos/app/App';

// Import tool store for hydration
import { oikosToolStore, type OikosToolCall } from '../oikos/lib/oikos-tool-store';
import { parseUTC } from '../lib/dateUtils';

// API functions
import { fetchThreadByTitle, fetchThreadMessages, request, fetchSystemCapabilities } from '../services/api';

// Components
import { ApiKeyModal } from '../components/ApiKeyModal';
import '../components/ApiKeyModal.css';

const DEMO_THREAD_TITLES: Record<string, string> = {
  'oikos-math': '[scenario:oikos-math] Oikos math (2+2)',
};

export default function OikosChatPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const threadTitle = searchParams.get('thread');
  const demoScenario = searchParams.get('demo');
  const [initialMessages, setInitialMessages] = useState<ChatMessage[] | undefined>(undefined);
  const [loading, setLoading] = useState(!!threadTitle || !!demoScenario);
  const { showSessionPicker } = useSessionPicker();
  const isE2E = import.meta.env.VITE_E2E === 'true';

  // API key availability state
  const [llmAvailable, setLlmAvailable] = useState<boolean | null>(null);
  const [showApiKeyModal, setShowApiKeyModal] = useState(false);

  // Check system capabilities on mount
  useEffect(() => {
    let cancelled = false;

    const checkCapabilities = async () => {
      try {
        const caps = await fetchSystemCapabilities();
        if (!cancelled) {
          setLlmAvailable(caps.llm_available);
          // Show modal if LLM is not available
          const isReplay = !!(window as Window & { __REPLAY_SCENARIO?: string }).__REPLAY_SCENARIO;
          if (!caps.llm_available && !isE2E && !isReplay) {
            setShowApiKeyModal(true);
          }
        }
      } catch (error) {
        // If capabilities endpoint fails, assume LLM is available (fail open)
        console.warn('Failed to fetch system capabilities:', error);
        if (!cancelled) {
          setLlmAvailable(true);
        }
      }
    };

    void checkCapabilities();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleOpenIntegrations = useCallback(() => {
    setShowApiKeyModal(false);
    navigate('/settings/integrations');
  }, [navigate]);

  // Handle session picker event from oikos
  const handleShowSessionPicker = useCallback(
    async (data: { runId: number; filters?: { project?: string; query?: string; provider?: string }; timestamp: number }) => {
      const result = await showSessionPicker({
        filters: data.filters,
        showStartNew: false,
      });

      if (result.sessionId) {
        // Navigate to Forum with session selected and chat open
        navigate(`/forum?session=${result.sessionId}&chat=true`);
      }
    },
    [showSessionPicker, navigate]
  );

  // Subscribe to session picker event
  useEffect(() => {
    const unsubscribe = eventBus.on('oikos:show_session_picker', handleShowSessionPicker);
    return () => unsubscribe();
  }, [handleShowSessionPicker]);

  useEffect(() => {
    if (!demoScenario) return;

    const storageKey = `oikos-demo-seeded:${demoScenario}`;
    if (typeof window !== 'undefined' && window.sessionStorage.getItem(storageKey)) {
      if (!threadTitle && DEMO_THREAD_TITLES[demoScenario]) {
        const nextParams = new URLSearchParams(searchParams);
        nextParams.set('thread', DEMO_THREAD_TITLES[demoScenario]);
        setSearchParams(nextParams, { replace: true });
      }
      return;
    }

    let cancelled = false;

    const seedScenario = async () => {
      try {
        await request('/scenarios/seed', {
          method: 'POST',
          body: JSON.stringify({ name: demoScenario, clean: true }),
        });
        if (typeof window !== 'undefined') {
          window.sessionStorage.setItem(storageKey, '1');
        }
        if (!threadTitle && DEMO_THREAD_TITLES[demoScenario]) {
          const nextParams = new URLSearchParams(searchParams);
          nextParams.set('thread', DEMO_THREAD_TITLES[demoScenario]);
          setSearchParams(nextParams, { replace: true });
        }
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn('Oikos demo seeding failed:', error);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void seedScenario();

    return () => {
      cancelled = true;
    };
  }, [demoScenario, threadTitle, searchParams, setSearchParams]);

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
        oikosToolStore.clearTools();

        // Collect tools to hydrate into the store
        const toolsToLoad: OikosToolCall[] = [];

        // Convert backend ThreadMessages to Oikos ChatMessages
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

            // Convert tool_calls to OikosToolCall format for the store
            if (toolCalls && syntheticRunId !== undefined) {
              for (const tc of toolCalls) {
                toolsToLoad.push({
                  toolCallId: tc.id,
                  toolName: tc.name,
                  status: 'completed', // Historical tools are always completed
                  runId: syntheticRunId,
                  startedAt: m.sent_at ? parseUTC(m.sent_at).getTime() : Date.now(),
                  completedAt: m.sent_at ? parseUTC(m.sent_at).getTime() : Date.now(),
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
              timestamp: m.sent_at ? parseUTC(m.sent_at) : new Date(),
              skipAnimation: true, // Don't animate pre-loaded messages
              runId: syntheticRunId,
              toolCalls,
            };
          });

        // Hydrate the tool store with historical tools
        if (toolsToLoad.length > 0) {
          oikosToolStore.loadTools(toolsToLoad);
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
      <div className="oikos-container oikos-loading">
        <div className="oikos-loading-text">Loading conversation...</div>
      </div>
    );
  }

  return (
    <AppProvider initialMessages={initialMessages}>
      <div className="oikos-container">
        <App embedded={true} />
      </div>
      <ApiKeyModal
        isOpen={showApiKeyModal}
        onClose={() => setShowApiKeyModal(false)}
        onOpenIntegrations={handleOpenIntegrations}
      />
    </AppProvider>
  );
}
