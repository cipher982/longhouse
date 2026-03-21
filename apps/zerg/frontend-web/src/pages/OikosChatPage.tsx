/**
 * OikosChatPage - Entry point for the Oikos chat UI within the Zerg SPA
 *
 * This wraps the Oikos React app (formerly standalone) in its context provider
 * and loads the Oikos-specific CSS styles.
 *
 * Supports URL params:
 * - ?thread=<title> - Load a backend thread by title for display
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
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
import {
  fetchThreadByTitle,
  fetchThreadMessages,
  request,
  fetchSystemCapabilities,
  type ThreadMessage,
} from '../services/api';

// Components
import { ApiKeyModal } from '../components/ApiKeyModal';
import '../components/ApiKeyModal.css';

const DEMO_THREAD_TITLES: Record<string, string> = {
  'oikos-math': '[scenario:oikos-math] Oikos math (2+2)',
};

interface HydratedThreadBootstrap {
  initialMessages: ChatMessage[];
  toolsToLoad: OikosToolCall[];
}

function hydrateThreadBootstrap(messages: ThreadMessage[]): HydratedThreadBootstrap {
  const toolsToLoad: OikosToolCall[] = [];

  const initialMessages: ChatMessage[] = messages
    .filter((message) => message.role === 'user' || message.role === 'assistant')
    .map((message) => {
      const apiToolCalls = (
        message as { tool_calls?: Array<{ id: string; name: string; args: Record<string, unknown> }> }
      ).tool_calls;

      const toolCalls: StoredToolCall[] | undefined =
        apiToolCalls && apiToolCalls.length > 0
          ? apiToolCalls.map((toolCall) => ({
              id: toolCall.id,
              name: toolCall.name,
              args: toolCall.args || {},
            }))
          : undefined;

      const syntheticRunId = toolCalls ? -message.id : undefined;
      const toolTimestamp = message.sent_at ? parseUTC(message.sent_at).getTime() : Date.now();

      if (toolCalls && syntheticRunId !== undefined) {
        for (const toolCall of toolCalls) {
          toolsToLoad.push({
            toolCallId: toolCall.id,
            toolName: toolCall.name,
            status: 'completed',
            runId: syntheticRunId,
            startedAt: toolTimestamp,
            completedAt: toolTimestamp,
            argsPreview: JSON.stringify(toolCall.args).slice(0, 100),
            args: toolCall.args,
            logs: [],
          });
        }
      }

      return {
        id: String(message.id),
        role: message.role as 'user' | 'assistant',
        content: message.content || '',
        timestamp: message.sent_at ? parseUTC(message.sent_at) : new Date(),
        skipAnimation: true,
        runId: syntheticRunId,
        toolCalls,
      };
    });

  return {
    initialMessages,
    toolsToLoad,
  };
}

export default function OikosChatPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const threadTitle = searchParams.get('thread');
  const demoScenario = searchParams.get('demo');
  const [isDemoSeeding, setIsDemoSeeding] = useState(Boolean(demoScenario));
  const { showSessionPicker } = useSessionPicker();
  const isE2E = import.meta.env.VITE_E2E === 'true';
  const isReplay = !!(window as Window & { __REPLAY_SCENARIO?: string }).__REPLAY_SCENARIO;

  // API key availability state
  const [apiKeyModalDismissed, setApiKeyModalDismissed] = useState(false);

  const capabilitiesQuery = useQuery({
    queryKey: ['system-capabilities'],
    queryFn: async () => {
      try {
        return await fetchSystemCapabilities();
      } catch (error) {
        console.warn('Failed to fetch system capabilities:', error);
        return {
          llm_available: true,
          auth_disabled: false,
        };
      }
    },
    retry: false,
    staleTime: 60_000,
  });

  const llmAvailable = capabilitiesQuery.data?.llm_available ?? null;
  const showApiKeyModal =
    llmAvailable === false && !isE2E && !isReplay && !apiKeyModalDismissed;

  const preloadedThreadQuery = useQuery({
    queryKey: ['oikos-thread', threadTitle],
    queryFn: () => fetchThreadByTitle(threadTitle as string),
    enabled: Boolean(threadTitle),
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const preloadedThreadId = preloadedThreadQuery.data?.id ?? null;
  const preloadedMessagesQuery = useQuery({
    queryKey: ['oikos-thread-messages', preloadedThreadId],
    queryFn: () => fetchThreadMessages(preloadedThreadId as number),
    enabled: preloadedThreadId !== null,
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const hydratedThreadBootstrap = useMemo<HydratedThreadBootstrap>(
    () =>
      preloadedMessagesQuery.data
        ? hydrateThreadBootstrap(preloadedMessagesQuery.data)
        : { initialMessages: [], toolsToLoad: [] },
    [preloadedMessagesQuery.data],
  );

  const initialMessages =
    threadTitle && preloadedMessagesQuery.data ? hydratedThreadBootstrap.initialMessages : undefined;

  const appProviderKey = threadTitle
    ? `oikos-thread:${preloadedThreadId ?? threadTitle}:${preloadedMessagesQuery.dataUpdatedAt}`
    : 'oikos-live';

  const loading =
    isDemoSeeding
    || (Boolean(threadTitle)
      && (preloadedThreadQuery.isLoading || (preloadedThreadId !== null && preloadedMessagesQuery.isLoading)));

  const handleOpenIntegrations = useCallback(() => {
    setApiKeyModalDismissed(true);
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
        // Forum is temporarily disabled; route directly to session detail.
        navigate(`/timeline/${result.sessionId}`);
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
    if (!demoScenario) {
      setIsDemoSeeding(false);
      return;
    }

    const storageKey = `oikos-demo-seeded:${demoScenario}`;
    if (typeof window !== 'undefined' && window.sessionStorage.getItem(storageKey)) {
      if (!threadTitle && DEMO_THREAD_TITLES[demoScenario]) {
        const nextParams = new URLSearchParams(searchParams);
        nextParams.set('thread', DEMO_THREAD_TITLES[demoScenario]);
        setSearchParams(nextParams, { replace: true });
      }
      setIsDemoSeeding(false);
      return;
    }

    let cancelled = false;
    setIsDemoSeeding(true);

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
        console.warn('Oikos demo seeding failed:', error);
      } finally {
        if (!cancelled) {
          setIsDemoSeeding(false);
        }
      }
    };

    void seedScenario();

    return () => {
      cancelled = true;
    };
  }, [demoScenario, threadTitle, searchParams, setSearchParams]);

  useEffect(() => {
    if (preloadedThreadQuery.isLoading || preloadedMessagesQuery.isLoading) {
      return;
    }

    oikosToolStore.clearTools();
    if (hydratedThreadBootstrap.toolsToLoad.length > 0) {
      oikosToolStore.loadTools(hydratedThreadBootstrap.toolsToLoad);
    }
  }, [
    hydratedThreadBootstrap,
    preloadedMessagesQuery.isLoading,
    preloadedThreadQuery.isLoading,
  ]);

  // Show loading state while fetching thread
  if (loading) {
    return (
      <div className="oikos-container oikos-loading">
        <div className="oikos-loading-text">Loading conversation...</div>
      </div>
    );
  }

  return (
    <AppProvider key={appProviderKey} initialMessages={initialMessages}>
      <div className="oikos-container">
        <App embedded={true} />
      </div>
      <ApiKeyModal
        isOpen={showApiKeyModal}
        onClose={() => setApiKeyModalDismissed(true)}
        onOpenIntegrations={handleOpenIntegrations}
      />
    </AppProvider>
  );
}
