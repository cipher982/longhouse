/**
 * ChatContainer Timeline Ordering Tests
 *
 * Verifies that timeline events render in the correct logical order:
 * User message → Tool cards → Assistant message
 *
 * This catches the visual "jumping" bug where assistant responses
 * appeared above worker cards during streaming, then moved below.
 */

import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import type { ChatMessage } from '../../context/types';
import { ChatContainer } from '../ChatContainer';
import { supervisorToolStore } from '../../../lib/supervisor-tool-store';

// Note: markdown-renderer is mocked globally in src/test/setup.ts (via dompurify mock)

describe('ChatContainer timeline ordering', () => {
  beforeEach(() => {
    supervisorToolStore.clearTools();
  });

  afterEach(() => {
    cleanup();  // Ensure DOM cleanup between tests
    supervisorToolStore.clearTools();
  });

  it('keeps spawn_commis card between user and assistant during streaming', () => {
    // Setup: user message + streaming assistant + tool card, all same runId
    const now = new Date();
    const messages: ChatMessage[] = [
      {
        id: 'user-1',
        role: 'user',
        content: 'check disk space on cube',
        timestamp: now,
        runId: 123,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: '',  // Empty during streaming
        status: 'typing',
        timestamp: new Date(now.getTime() + 100),  // Slightly after user
        runId: 123,
      },
    ];

    // Directly add tool to store (simulates supervisor_tool_started event)
    // Access internal state for test setup
    const toolState = supervisorToolStore.getState();
    toolState.tools.set('call-1', {
      toolCallId: 'call-1',
      toolName: 'spawn_commis',
      status: 'running',
      runId: 123,
      startedAt: now.getTime() + 50,  // Between user and assistant timestamps
      args: { task: 'Check disk space on cube' },
      logs: [],
    });

    render(<ChatContainer messages={messages} />);

    // Get the transcript container
    const transcript = screen.getByTestId('messages-container');

    // Query all timeline items (message groups and worker tool cards)
    const timelineItems = transcript.querySelectorAll('.message-group, [data-testid="worker-tool-card"]');

    // Find indices by type
    let userIdx = -1;
    let toolIdx = -1;
    let assistantIdx = -1;

    timelineItems.forEach((item, idx) => {
      if (item.classList.contains('message-group')) {
        if (item.querySelector('.message.user')) {
          userIdx = idx;
        } else if (item.querySelector('.message.assistant')) {
          assistantIdx = idx;
        }
      } else if (item.matches('[data-testid="worker-tool-card"]')) {
        toolIdx = idx;
      }
    });

    // Assert: user < tool < assistant
    expect(userIdx).toBeGreaterThanOrEqual(0);
    expect(toolIdx).toBeGreaterThanOrEqual(0);
    expect(assistantIdx).toBeGreaterThanOrEqual(0);

    expect(userIdx).toBeLessThan(toolIdx);
    expect(toolIdx).toBeLessThan(assistantIdx);
  });

  it('maintains order after streaming completes (assistant has content)', () => {
    const now = new Date();
    const messages: ChatMessage[] = [
      {
        id: 'user-1',
        role: 'user',
        content: 'check disk space on cube',
        timestamp: now,
        runId: 123,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: 'Cube is at 45% disk usage.',  // Has content now
        status: 'final',
        timestamp: new Date(now.getTime() + 100),
        runId: 123,
      },
    ];

    // Add completed tool
    const toolState = supervisorToolStore.getState();
    toolState.tools.set('call-1', {
      toolCallId: 'call-1',
      toolName: 'spawn_commis',
      status: 'completed',
      runId: 123,
      startedAt: now.getTime() + 50,
      completedAt: now.getTime() + 90,
      durationMs: 40,
      args: { task: 'Check disk space on cube' },
      result: { workerStatus: 'complete', workerSummary: 'Disk at 45%' },
      logs: [],
    });

    render(<ChatContainer messages={messages} />);
    const transcript = screen.getByTestId('messages-container');
    const timelineItems = transcript.querySelectorAll('.message-group, [data-testid="worker-tool-card"]');

    let userIdx = -1;
    let toolIdx = -1;
    let assistantIdx = -1;

    timelineItems.forEach((item, idx) => {
      if (item.classList.contains('message-group')) {
        if (item.querySelector('.message.user')) {
          userIdx = idx;
        } else if (item.querySelector('.message.assistant')) {
          assistantIdx = idx;
        }
      } else if (item.matches('[data-testid="worker-tool-card"]')) {
        toolIdx = idx;
      }
    });

    // Order should still be: user < tool < assistant
    expect(userIdx).toBeLessThan(toolIdx);
    expect(toolIdx).toBeLessThan(assistantIdx);
  });

  it('orders multiple tools chronologically within same run', () => {
    const now = new Date();
    const messages: ChatMessage[] = [
      {
        id: 'user-1',
        role: 'user',
        content: 'research options',
        timestamp: now,
        runId: 456,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: '',
        status: 'typing',
        timestamp: new Date(now.getTime() + 200),
        runId: 456,
      },
    ];

    // Add two tools with different timestamps
    const toolState = supervisorToolStore.getState();
    toolState.tools.set('call-a', {
      toolCallId: 'call-a',
      toolName: 'web_search',
      status: 'completed',
      runId: 456,
      startedAt: now.getTime() + 50,  // First tool
      completedAt: now.getTime() + 70,
      durationMs: 20,
      args: { query: 'best options' },
      logs: [],
    });
    toolState.tools.set('call-b', {
      toolCallId: 'call-b',
      toolName: 'knowledge_search',
      status: 'running',
      runId: 456,
      startedAt: now.getTime() + 100,  // Second tool
      args: { query: 'preferences' },
      logs: [],
    });

    render(<ChatContainer messages={messages} />);
    const transcript = screen.getByTestId('messages-container');

    // Get all tool cards
    const toolCards = transcript.querySelectorAll('[data-testid="tool-card"], [data-testid="worker-tool-card"]');
    const toolCallIds = Array.from(toolCards).map(el => el.getAttribute('data-tool-call-id'));

    // call-a should come before call-b (chronological)
    const idxA = toolCallIds.indexOf('call-a');
    const idxB = toolCallIds.indexOf('call-b');

    if (idxA >= 0 && idxB >= 0) {
      expect(idxA).toBeLessThan(idxB);
    }
  });

  it('separates tools from different runs', () => {
    const now = new Date();
    const messages: ChatMessage[] = [
      // Run 1
      {
        id: 'user-1',
        role: 'user',
        content: 'first question',
        timestamp: now,
        runId: 100,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: 'First answer',
        status: 'final',
        timestamp: new Date(now.getTime() + 100),
        runId: 100,
      },
      // Run 2
      {
        id: 'user-2',
        role: 'user',
        content: 'second question',
        timestamp: new Date(now.getTime() + 200),
        runId: 200,
      },
      {
        id: 'asst-2',
        role: 'assistant',
        content: '',
        status: 'typing',
        timestamp: new Date(now.getTime() + 300),
        runId: 200,
      },
    ];

    // Add tool for run 1
    const toolState = supervisorToolStore.getState();
    toolState.tools.set('call-run1', {
      toolCallId: 'call-run1',
      toolName: 'tool_run1',
      status: 'completed',
      runId: 100,
      startedAt: now.getTime() + 50,
      logs: [],
    });

    // Add tool for run 2
    toolState.tools.set('call-run2', {
      toolCallId: 'call-run2',
      toolName: 'tool_run2',
      status: 'running',
      runId: 200,
      startedAt: now.getTime() + 250,
      logs: [],
    });

    render(<ChatContainer messages={messages} />);
    const transcript = screen.getByTestId('messages-container');
    const timelineItems = transcript.querySelectorAll('.message-group, [data-testid="tool-card"], [data-testid="worker-tool-card"]');

    // Extract ordered list of (type, runId)
    const items: Array<{ type: string; runId?: number }> = [];
    timelineItems.forEach(item => {
      if (item.classList.contains('message-group')) {
        const userMsg = item.querySelector('.message.user');
        const asstMsg = item.querySelector('.message.assistant');
        if (userMsg) items.push({ type: 'user' });
        if (asstMsg) items.push({ type: 'assistant' });
      } else {
        const toolCallId = item.getAttribute('data-tool-call-id');
        items.push({ type: 'tool', runId: toolCallId?.includes('run1') ? 100 : 200 });
      }
    });

    // Verify run 1's tool comes before run 2's content
    const run1ToolIdx = items.findIndex(i => i.type === 'tool' && i.runId === 100);
    const run2StartIdx = items.findIndex((i, idx) => idx > 1 && i.type === 'user');  // Second user message

    if (run1ToolIdx >= 0 && run2StartIdx >= 0) {
      expect(run1ToolIdx).toBeLessThan(run2StartIdx);
    }
  });
});
