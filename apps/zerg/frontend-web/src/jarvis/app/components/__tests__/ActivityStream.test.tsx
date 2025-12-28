import { describe, expect, it, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ActivityStream } from '../ActivityStream';
import { supervisorToolStore } from '../../../lib/supervisor-tool-store';
import { eventBus } from '../../../lib/event-bus';
import type { SupervisorToolCall } from '../../../lib/supervisor-tool-store';

describe('ActivityStream', () => {
  beforeEach(() => {
    supervisorToolStore.clearTools();
  });

  describe('rendering', () => {
    it('renders nothing when no tools exist', () => {
      const { container } = render(<ActivityStream runId={1} />);
      expect(container.firstChild).toBeNull();
    });

    it('renders nothing when runId is null', () => {
      // Add a tool
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      const { container } = render(<ActivityStream runId={null} />);
      expect(container.firstChild).toBeNull();
    });

    it('renders tool cards for matching runId', () => {
      // Add tools for run 1
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'get_current_location',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'web_search',
        toolCallId: 'call-2',
        timestamp: Date.now(),
      });

      const { container } = render(<ActivityStream runId={1} />);

      // Both tools should be visible
      expect(container).toHaveTextContent('get_current_location');
      expect(container).toHaveTextContent('web_search');
    });

    it('filters tools by runId', () => {
      // Add tools for different runs
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'tool_run_1',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      eventBus.emit('supervisor:tool_started', {
        runId: 2,
        toolName: 'tool_run_2',
        toolCallId: 'call-2',
        timestamp: Date.now(),
      });

      const { container } = render(<ActivityStream runId={1} />);

      // Only run 1 tool should be visible
      expect(container).toHaveTextContent('tool_run_1');
      expect(container).not.toHaveTextContent('tool_run_2');
    });

    it('displays tools in chronological order', () => {
      const now = Date.now();

      // Add tools out of order
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'tool_b',
        toolCallId: 'call-2',
        timestamp: now + 1000,
      });

      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: now,
      });

      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'tool_c',
        toolCallId: 'call-3',
        timestamp: now + 2000,
      });

      const { container } = render(<ActivityStream runId={1} />);

      // Get all tool cards
      const toolCards = container.querySelectorAll('.tool-card');
      expect(toolCards).toHaveLength(3);

      // Check order by tool name
      expect(toolCards[0]).toHaveTextContent('tool_a');
      expect(toolCards[1]).toHaveTextContent('tool_b');
      expect(toolCards[2]).toHaveTextContent('tool_c');
    });
  });

  describe('CSS classes', () => {
    it('applies active class when tools are running', () => {
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      const { container } = render(<ActivityStream runId={1} />);

      expect(container.querySelector('.activity-stream--active')).toBeInTheDocument();
    });

    it('removes active class when all tools complete', () => {
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      const { container, rerender } = render(<ActivityStream runId={1} />);
      expect(container.querySelector('.activity-stream--active')).toBeInTheDocument();

      // Complete the tool
      eventBus.emit('supervisor:tool_completed', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        durationMs: 1000,
        timestamp: Date.now(),
      });

      // Force re-render to pick up state change
      rerender(<ActivityStream runId={1} />);

      expect(container.querySelector('.activity-stream--active')).not.toBeInTheDocument();
    });

    it('applies custom className prop', () => {
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      const { container } = render(<ActivityStream runId={1} className="custom-class" />);

      expect(container.querySelector('.custom-class')).toBeInTheDocument();
    });
  });

  describe('reactivity', () => {
    it('updates when tools are added', () => {
      const { container, rerender } = render(<ActivityStream runId={1} />);

      // Initially empty
      expect(container.firstChild).toBeNull();

      // Add a tool
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      // Force re-render
      rerender(<ActivityStream runId={1} />);

      // Tool should now be visible
      expect(container).toHaveTextContent('test_tool');
    });

    it('updates when tool status changes', () => {
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      const { container, rerender } = render(<ActivityStream runId={1} />);

      // Should show running status
      expect(container).toHaveTextContent('⏳');

      // Complete the tool
      eventBus.emit('supervisor:tool_completed', {
        runId: 1,
        toolName: 'test_tool',
        toolCallId: 'call-1',
        durationMs: 1000,
        timestamp: Date.now(),
      });

      rerender(<ActivityStream runId={1} />);

      // Should show completed status
      expect(container).toHaveTextContent('✓');
      expect(container).not.toHaveTextContent('⏳');
    });
  });

  describe('persistence', () => {
    it('displays loaded tools from history', () => {
      const historicalTools: SupervisorToolCall[] = [
        {
          toolCallId: 'call-1',
          toolName: 'historical_tool',
          status: 'completed',
          runId: 1,
          startedAt: Date.now() - 5000,
          completedAt: Date.now() - 3000,
          durationMs: 2000,
          logs: [],
        },
      ];

      supervisorToolStore.loadTools(historicalTools);

      const { container } = render(<ActivityStream runId={1} />);

      expect(container).toHaveTextContent('historical_tool');
      expect(container).toHaveTextContent('✓');
    });

    it('combines historical and live tools', () => {
      // Load historical tool
      const historicalTools: SupervisorToolCall[] = [
        {
          toolCallId: 'call-1',
          toolName: 'historical_tool',
          status: 'completed',
          runId: 1,
          startedAt: Date.now() - 5000,
          completedAt: Date.now() - 3000,
          durationMs: 2000,
          logs: [],
        },
      ];

      supervisorToolStore.loadTools(historicalTools);

      // Add live tool
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'live_tool',
        toolCallId: 'call-2',
        timestamp: Date.now(),
      });

      const { container } = render(<ActivityStream runId={1} />);

      // Both should be visible
      expect(container).toHaveTextContent('historical_tool');
      expect(container).toHaveTextContent('live_tool');
    });
  });

  describe('multiple runs', () => {
    it('shows correct tools when runId changes', () => {
      // Add tools for run 1
      eventBus.emit('supervisor:tool_started', {
        runId: 1,
        toolName: 'tool_run_1',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      // Add tools for run 2
      eventBus.emit('supervisor:tool_started', {
        runId: 2,
        toolName: 'tool_run_2',
        toolCallId: 'call-2',
        timestamp: Date.now(),
      });

      const { container, rerender } = render(<ActivityStream runId={1} />);

      // Should show run 1 tool
      expect(container).toHaveTextContent('tool_run_1');
      expect(container).not.toHaveTextContent('tool_run_2');

      // Change to run 2
      rerender(<ActivityStream runId={2} />);

      // Should show run 2 tool
      expect(container).toHaveTextContent('tool_run_2');
      expect(container).not.toHaveTextContent('tool_run_1');
    });
  });
});
