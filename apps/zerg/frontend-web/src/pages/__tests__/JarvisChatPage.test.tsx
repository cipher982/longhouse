/**
 * JarvisChatPage Tests
 *
 * Tests for the Jarvis chat page, specifically focusing on:
 * - Tool call hydration from backend on page load
 * - Ensuring tool calls persist after refresh (regression test)
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestRouter } from '../../test/test-utils';
import { SessionPickerProvider } from '../../components/SessionPickerProvider';
import JarvisChatPage from '../JarvisChatPage';
import { supervisorToolStore } from '../../jarvis/lib/supervisor-tool-store';

// Mock the API functions
const apiMocks = vi.hoisted(() => ({
  fetchThreadByTitle: vi.fn(),
  fetchThreadMessages: vi.fn(),
}));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    ...apiMocks,
  };
});

const { fetchThreadByTitle: mockFetchThreadByTitle, fetchThreadMessages: mockFetchThreadMessages } = apiMocks;

function renderJarvisChatPage(initialEntry = '/chat') {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionPickerProvider>
        <TestRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/chat" element={<JarvisChatPage />} />
          </Routes>
        </TestRouter>
      </SessionPickerProvider>
    </QueryClientProvider>
  );
}

describe('JarvisChatPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    supervisorToolStore.clearTools();
  });

  afterEach(() => {
    cleanup();
    supervisorToolStore.clearTools();
  });

  describe('Tool Call Hydration', () => {
    it('hydrates tool calls from API response into supervisor tool store', async () => {
      const now = new Date().toISOString();

      // Mock thread lookup
      mockFetchThreadByTitle.mockResolvedValue({
        id: 1,
        agent_id: 1,
        title: 'test-thread',
        active: true,
        created_at: now,
        updated_at: now,
        messages: [],
      });

      // Mock messages with tool_calls (assistant message that made tool calls)
      mockFetchThreadMessages.mockResolvedValue([
        {
          id: 1,
          thread_id: 1,
          role: 'user',
          content: "What's the weather?",
          sent_at: now,
          processed: true,
        },
        {
          id: 2,
          thread_id: 1,
          role: 'assistant',
          content: 'Let me check the weather for you.',
          sent_at: now,
          processed: true,
          // Tool calls in LangChain format (as stored in DB)
          tool_calls: [
            {
              id: 'call_abc123',
              name: 'get_weather',
              args: { location: 'San Francisco' },
            },
          ],
        },
        {
          id: 3,
          thread_id: 1,
          role: 'tool',
          content: '{"temp": 72, "condition": "sunny"}',
          tool_call_id: 'call_abc123',
          name: 'get_weather',
          sent_at: now,
          processed: true,
        },
        {
          id: 4,
          thread_id: 1,
          role: 'assistant',
          content: "It's 72°F and sunny in San Francisco!",
          sent_at: now,
          processed: true,
        },
      ]);

      renderJarvisChatPage('/chat?thread=test-thread');

      // Wait for the thread to load
      await waitFor(() => {
        expect(mockFetchThreadByTitle).toHaveBeenCalledWith('test-thread');
      });

      // Wait for messages to load
      await waitFor(() => {
        expect(mockFetchThreadMessages).toHaveBeenCalledWith(1);
      });

      // Verify the tool store was populated with the tool call
      await waitFor(() => {
        const state = supervisorToolStore.getState();
        expect(state.tools.size).toBe(1);

        // Check the tool was hydrated correctly
        const tool = state.tools.get('call_abc123');
        expect(tool).toBeDefined();
        expect(tool?.toolName).toBe('get_weather');
        expect(tool?.status).toBe('completed');
        expect(tool?.args).toEqual({ location: 'San Francisco' });
        // Synthetic runId should be negative message ID
        expect(tool?.runId).toBe(-2);
      });
    });

    it('hydrates multiple tool calls from a single message', async () => {
      const now = new Date().toISOString();

      mockFetchThreadByTitle.mockResolvedValue({
        id: 1,
        agent_id: 1,
        title: 'multi-tool-thread',
        active: true,
        created_at: now,
        updated_at: now,
        messages: [],
      });

      mockFetchThreadMessages.mockResolvedValue([
        {
          id: 1,
          thread_id: 1,
          role: 'user',
          content: 'Check weather and my location',
          sent_at: now,
          processed: true,
        },
        {
          id: 2,
          thread_id: 1,
          role: 'assistant',
          content: 'Checking...',
          sent_at: now,
          processed: true,
          tool_calls: [
            { id: 'call_weather', name: 'get_weather', args: { location: 'NYC' } },
            { id: 'call_location', name: 'get_current_location', args: {} },
          ],
        },
      ]);

      renderJarvisChatPage('/chat?thread=multi-tool-thread');

      await waitFor(() => {
        const state = supervisorToolStore.getState();
        expect(state.tools.size).toBe(2);

        const weatherTool = state.tools.get('call_weather');
        const locationTool = state.tools.get('call_location');

        expect(weatherTool?.toolName).toBe('get_weather');
        expect(locationTool?.toolName).toBe('get_current_location');

        // Both should have same synthetic runId (from same message)
        expect(weatherTool?.runId).toBe(-2);
        expect(locationTool?.runId).toBe(-2);
      });
    });

    it('handles messages without tool_calls gracefully', async () => {
      const now = new Date().toISOString();

      mockFetchThreadByTitle.mockResolvedValue({
        id: 1,
        agent_id: 1,
        title: 'no-tools-thread',
        active: true,
        created_at: now,
        updated_at: now,
        messages: [],
      });

      // Messages with no tool_calls
      mockFetchThreadMessages.mockResolvedValue([
        {
          id: 1,
          thread_id: 1,
          role: 'user',
          content: 'Hello',
          sent_at: now,
          processed: true,
        },
        {
          id: 2,
          thread_id: 1,
          role: 'assistant',
          content: 'Hello! How can I help?',
          sent_at: now,
          processed: true,
          // No tool_calls
        },
      ]);

      renderJarvisChatPage('/chat?thread=no-tools-thread');

      await waitFor(() => {
        expect(mockFetchThreadMessages).toHaveBeenCalled();
      });

      // Tool store should be empty
      await waitFor(() => {
        const state = supervisorToolStore.getState();
        expect(state.tools.size).toBe(0);
      });
    });

    it('clears tool store when loading a different thread', async () => {
      const now = new Date().toISOString();

      // Pre-populate the store with some tools
      supervisorToolStore.loadTools([
        {
          toolCallId: 'old_call',
          toolName: 'old_tool',
          status: 'completed',
          runId: -999,
          startedAt: Date.now(),
          logs: [],
        },
      ]);

      expect(supervisorToolStore.getState().tools.size).toBe(1);

      mockFetchThreadByTitle.mockResolvedValue({
        id: 2,
        agent_id: 1,
        title: 'new-thread',
        active: true,
        created_at: now,
        updated_at: now,
        messages: [],
      });

      mockFetchThreadMessages.mockResolvedValue([
        {
          id: 10,
          thread_id: 2,
          role: 'assistant',
          content: 'New thread message',
          sent_at: now,
          processed: true,
          tool_calls: [{ id: 'new_call', name: 'new_tool', args: {} }],
        },
      ]);

      renderJarvisChatPage('/chat?thread=new-thread');

      await waitFor(() => {
        const state = supervisorToolStore.getState();
        // Old tool should be cleared, only new tool present
        expect(state.tools.size).toBe(1);
        expect(state.tools.has('old_call')).toBe(false);
        expect(state.tools.has('new_call')).toBe(true);
      });
    });

    it('displays tool cards for hydrated tools via ActivityStream', async () => {
      const now = new Date().toISOString();

      mockFetchThreadByTitle.mockResolvedValue({
        id: 1,
        agent_id: 1,
        title: 'display-test',
        active: true,
        created_at: now,
        updated_at: now,
        messages: [],
      });

      mockFetchThreadMessages.mockResolvedValue([
        {
          id: 1,
          thread_id: 1,
          role: 'user',
          content: 'Test',
          sent_at: now,
          processed: true,
        },
        {
          id: 2,
          thread_id: 1,
          role: 'assistant',
          content: 'Processing...',
          sent_at: now,
          processed: true,
          tool_calls: [
            { id: 'call_123', name: 'get_whoop_data', args: { metric: 'recovery' } },
          ],
        },
      ]);

      renderJarvisChatPage('/chat?thread=display-test');

      // Wait for the tool card to appear
      // The ToolCard component renders the tool name
      await waitFor(() => {
        // The tool store should be populated
        expect(supervisorToolStore.getState().tools.size).toBe(1);
      });

      // The actual rendering of ToolCard happens via ActivityStream
      // which reads from the store. The tool name should be visible in the tool card
      await waitFor(() => {
        // Look for the tool name in the tool-card__name span
        const toolNameElement = document.querySelector('.tool-card__name');
        expect(toolNameElement).not.toBeNull();
        expect(toolNameElement?.textContent).toBe('get_whoop_data');
      });
    });
  });

  describe('Regression: Tool calls persist after page refresh', () => {
    it('shows tool calls when page is loaded with thread param (simulates refresh)', async () => {
      /**
       * This test verifies the fix for the bug where tool calls would show
       * during live chat but disappear after refreshing the page.
       *
       * The root cause was that on page refresh:
       * 1. API returns messages WITH tool_calls
       * 2. But JarvisChatPage wasn't extracting them or setting runId
       * 3. supervisorToolStore was empty (no SSE events to populate it)
       * 4. So ChatContainer found no tools to render
       *
       * The fix hydrates tool_calls from the API into the supervisor tool store.
       */
      const now = new Date().toISOString();

      mockFetchThreadByTitle.mockResolvedValue({
        id: 1,
        agent_id: 1,
        title: 'refresh-test',
        active: true,
        created_at: now,
        updated_at: now,
        messages: [],
      });

      // Simulate what the API returns after a refresh - messages with tool_calls
      mockFetchThreadMessages.mockResolvedValue([
        {
          id: 1,
          thread_id: 1,
          role: 'user',
          content: 'Check my health data',
          sent_at: now,
          processed: true,
        },
        {
          id: 2,
          thread_id: 1,
          role: 'assistant',
          content: "I'll check your WHOOP data.",
          sent_at: now,
          processed: true,
          tool_calls: [
            { id: 'call_whoop', name: 'get_whoop_data', args: { metric: 'all' } },
          ],
        },
        {
          id: 3,
          thread_id: 1,
          role: 'tool',
          content: '{"recovery": 85, "hrv": 68}',
          tool_call_id: 'call_whoop',
          name: 'get_whoop_data',
          sent_at: now,
          processed: true,
        },
        {
          id: 4,
          thread_id: 1,
          role: 'assistant',
          content: 'Your recovery score is 85% with HRV of 68ms.',
          sent_at: now,
          processed: true,
        },
      ]);

      // Render page as if user navigated directly (page refresh scenario)
      renderJarvisChatPage('/chat?thread=refresh-test');

      // Verify tool store is populated (the core fix)
      await waitFor(() => {
        const state = supervisorToolStore.getState();
        expect(state.tools.size).toBe(1);

        const tool = state.tools.get('call_whoop');
        expect(tool).toBeDefined();
        expect(tool?.toolName).toBe('get_whoop_data');
      });

      // Verify the user message and assistant response are shown
      await waitFor(() => {
        expect(screen.getByText(/Check my health data/)).toBeInTheDocument();
      });
    });
  });
});
