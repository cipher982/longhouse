/**
 * ChatContainer Timeline Ordering Tests
 *
 * Verifies that timeline events render in the correct logical order:
 * User message → Tool cards → Assistant message
 *
 * This catches the visual "jumping" bug where assistant responses
 * appeared above commis cards during streaming, then moved below.
 */

import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import type { ChatMessage } from '../../context/types';
import { ChatContainer } from '../ChatContainer';
import { conciergeToolStore } from '../../../lib/concierge-tool-store';

// Note: markdown-renderer is mocked globally in src/test/setup.ts (via dompurify mock)

describe('ChatContainer timeline ordering', () => {
  beforeEach(() => {
    conciergeToolStore.clearTools();
  });

  afterEach(() => {
    cleanup();  // Ensure DOM cleanup between tests
    conciergeToolStore.clearTools();
  });

  it('keeps spawn_commis card between user and assistant during streaming', () => {
    // Setup: user message + streaming assistant + tool card, all same courseId
    const now = new Date();
    const messages: ChatMessage[] = [
      {
        id: 'user-1',
        role: 'user',
        content: 'check disk space on cube',
        timestamp: now,
        courseId: 123,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: '',  // Empty during streaming
        status: 'typing',
        timestamp: new Date(now.getTime() + 100),  // Slightly after user
        courseId: 123,
      },
    ];

    // Directly add tool to store (simulates concierge_tool_started event)
    // Access internal state for test setup
    const toolState = conciergeToolStore.getState();
    toolState.tools.set('call-1', {
      toolCallId: 'call-1',
      toolName: 'spawn_commis',
      status: 'running',
      courseId: 123,
      startedAt: now.getTime() + 50,  // Between user and assistant timestamps
      args: { task: 'Check disk space on cube' },
      logs: [],
    });

    render(<ChatContainer messages={messages} />);

    // Get the transcript container
    const transcript = screen.getByTestId('messages-container');

    // Query all timeline items (message groups and commis tool cards)
    const timelineItems = transcript.querySelectorAll('.message-group, [data-testid="commis-tool-card"]');

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
      } else if (item.matches('[data-testid="commis-tool-card"]')) {
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
        courseId: 123,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: 'Cube is at 45% disk usage.',  // Has content now
        status: 'final',
        timestamp: new Date(now.getTime() + 100),
        courseId: 123,
      },
    ];

    // Add completed tool
    const toolState = conciergeToolStore.getState();
    toolState.tools.set('call-1', {
      toolCallId: 'call-1',
      toolName: 'spawn_commis',
      status: 'completed',
      courseId: 123,
      startedAt: now.getTime() + 50,
      completedAt: now.getTime() + 90,
      durationMs: 40,
      args: { task: 'Check disk space on cube' },
      result: { commisStatus: 'complete', commisSummary: 'Disk at 45%' },
      logs: [],
    });

    render(<ChatContainer messages={messages} />);
    const transcript = screen.getByTestId('messages-container');
    const timelineItems = transcript.querySelectorAll('.message-group, [data-testid="commis-tool-card"]');

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
      } else if (item.matches('[data-testid="commis-tool-card"]')) {
        toolIdx = idx;
      }
    });

    // Order should still be: user < tool < assistant
    expect(userIdx).toBeLessThan(toolIdx);
    expect(toolIdx).toBeLessThan(assistantIdx);
  });

  it('orders multiple tools chronologically within same course', () => {
    const now = new Date();
    const messages: ChatMessage[] = [
      {
        id: 'user-1',
        role: 'user',
        content: 'research options',
        timestamp: now,
        courseId: 456,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: '',
        status: 'typing',
        timestamp: new Date(now.getTime() + 200),
        courseId: 456,
      },
    ];

    // Add two tools with different timestamps
    const toolState = conciergeToolStore.getState();
    toolState.tools.set('call-a', {
      toolCallId: 'call-a',
      toolName: 'web_search',
      status: 'completed',
      courseId: 456,
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
      courseId: 456,
      startedAt: now.getTime() + 100,  // Second tool
      args: { query: 'preferences' },
      logs: [],
    });

    render(<ChatContainer messages={messages} />);
    const transcript = screen.getByTestId('messages-container');

    // Get all tool cards
    const toolCards = transcript.querySelectorAll('[data-testid="tool-card"], [data-testid="commis-tool-card"]');
    const toolCallIds = Array.from(toolCards).map(el => el.getAttribute('data-tool-call-id'));

    // call-a should come before call-b (chronological)
    const idxA = toolCallIds.indexOf('call-a');
    const idxB = toolCallIds.indexOf('call-b');

    if (idxA >= 0 && idxB >= 0) {
      expect(idxA).toBeLessThan(idxB);
    }
  });

  it('separates tools from different courses', () => {
    const now = new Date();
    const messages: ChatMessage[] = [
      // Course 1
      {
        id: 'user-1',
        role: 'user',
        content: 'first question',
        timestamp: now,
        courseId: 100,
      },
      {
        id: 'asst-1',
        role: 'assistant',
        content: 'First answer',
        status: 'final',
        timestamp: new Date(now.getTime() + 100),
        courseId: 100,
      },
      // Course 2
      {
        id: 'user-2',
        role: 'user',
        content: 'second question',
        timestamp: new Date(now.getTime() + 200),
        courseId: 200,
      },
      {
        id: 'asst-2',
        role: 'assistant',
        content: '',
        status: 'typing',
        timestamp: new Date(now.getTime() + 300),
        courseId: 200,
      },
    ];

    // Add tool for course 1
    const toolState = conciergeToolStore.getState();
    toolState.tools.set('call-course1', {
      toolCallId: 'call-course1',
      toolName: 'tool_course1',
      status: 'completed',
      courseId: 100,
      startedAt: now.getTime() + 50,
      logs: [],
    });

    // Add tool for course 2
    toolState.tools.set('call-course2', {
      toolCallId: 'call-course2',
      toolName: 'tool_course2',
      status: 'running',
      courseId: 200,
      startedAt: now.getTime() + 250,
      logs: [],
    });

    render(<ChatContainer messages={messages} />);
    const transcript = screen.getByTestId('messages-container');
    const timelineItems = transcript.querySelectorAll('.message-group, [data-testid="tool-card"], [data-testid="commis-tool-card"]');

    // Extract ordered list of (type, courseId)
    const items: Array<{ type: string; courseId?: number }> = [];
    timelineItems.forEach(item => {
      if (item.classList.contains('message-group')) {
        const userMsg = item.querySelector('.message.user');
        const asstMsg = item.querySelector('.message.assistant');
        if (userMsg) items.push({ type: 'user' });
        if (asstMsg) items.push({ type: 'assistant' });
      } else {
        const toolCallId = item.getAttribute('data-tool-call-id');
        items.push({ type: 'tool', courseId: toolCallId?.includes('course1') ? 100 : 200 });
      }
    });

    // Verify course 1's tool comes before course 2's content
    const course1ToolIdx = items.findIndex(i => i.type === 'tool' && i.courseId === 100);
    const course2StartIdx = items.findIndex((i, idx) => idx > 1 && i.type === 'user');  // Second user message

    if (course1ToolIdx >= 0 && course2StartIdx >= 0) {
      expect(course1ToolIdx).toBeLessThan(course2StartIdx);
    }
  });
});
