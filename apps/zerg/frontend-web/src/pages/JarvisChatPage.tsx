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

// Import Jarvis styles
import '../jarvis/styles/index.css';

// Import Jarvis app and context
import { AppProvider, type ChatMessage } from '../jarvis/app/context';
import App from '../jarvis/app/App';

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

        // Convert backend ThreadMessages to Jarvis ChatMessages
        // Filter to only user/assistant roles (skip system and tool messages)
        const chatMessages: ChatMessage[] = messages
          .filter((m) => m.role === 'user' || m.role === 'assistant')
          .map((m) => ({
            id: String(m.id),
            role: m.role as 'user' | 'assistant',
            content: m.content || '',
            timestamp: m.sent_at ? new Date(m.sent_at) : new Date(),
            skipAnimation: true, // Don't animate pre-loaded messages
          }));

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
      <div className="jarvis-container" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ color: '#9ca3af' }}>Loading conversation...</div>
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
