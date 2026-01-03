import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ToolCard } from '../ToolCard';
import type { SupervisorToolCall } from '../../../lib/supervisor-tool-store';

describe('ToolCard', () => {
  const baseToolCall: SupervisorToolCall = {
    toolCallId: 'call-123',
    toolName: 'get_current_location',
    status: 'running',
    runId: 1,
    startedAt: Date.now() - 5000, // Started 5 seconds ago
    logs: [],
  };

  describe('rendering states', () => {
    it('renders collapsed state by default', () => {
      const { container } = render(<ToolCard tool={baseToolCall} />);

      // Header elements should be visible
      expect(container).toHaveTextContent('get_current_location');
      expect(container).toHaveTextContent('📍'); // Icon
      expect(container).toHaveTextContent('⏳'); // Running status

      // Body should not be visible
      expect(container.querySelector('.tool-card__body')).not.toBeInTheDocument();
    });

    it('shows correct status icon for running tool', () => {
      const { container } = render(<ToolCard tool={{ ...baseToolCall, status: 'running' }} />);
      expect(container).toHaveTextContent('⏳');
    });

    it('shows correct status icon for completed tool', () => {
      const { container } = render(<ToolCard tool={{ ...baseToolCall, status: 'completed' }} />);
      expect(container).toHaveTextContent('✓');
    });

    it('shows correct status icon for failed tool', () => {
      const { container } = render(<ToolCard tool={{ ...baseToolCall, status: 'failed' }} />);
      expect(container).toHaveTextContent('✗');
    });

    it('displays live duration for running tools', () => {
      const { container } = render(<ToolCard tool={baseToolCall} />);
      // Should show duration in seconds (5.0s)
      expect(container).toHaveTextContent(/5\.0s/);
    });

    it('displays duration in milliseconds for sub-second durations', () => {
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        status: 'completed',
        startedAt: Date.now() - 500,
        completedAt: Date.now(),
        durationMs: 500,
      };

      const { container } = render(<ToolCard tool={tool} />);
      expect(container).toHaveTextContent('500ms');
    });

    it('displays args preview when available', () => {
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        argsPreview: 'device_id: "1"',
      };

      const { container } = render(<ToolCard tool={tool} />);
      expect(container).toHaveTextContent('device_id: "1"');
    });

    it('displays result preview on completion', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        status: 'completed',
        completedAt: Date.now(),
        durationMs: 1500,
        resultPreview: 'Location: San Francisco',
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Expand the card
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      expect(container).toHaveTextContent('Location: San Francisco');
    });

    it('displays error message on failure', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        status: 'failed',
        completedAt: Date.now(),
        durationMs: 500,
        error: 'API timeout',
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Expand the card
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      expect(container).toHaveTextContent('API timeout');
    });
  });

  describe('expansion behavior', () => {
    it('expands on click', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        argsPreview: 'test args',
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Body should not be visible initially
      expect(container.querySelector('.tool-card__body')).not.toBeInTheDocument();

      // Click to expand
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      // Body should now be visible
      expect(container.querySelector('.tool-card__body')).toBeInTheDocument();
    });

    it('collapses when clicked again', async () => {
      const user = userEvent.setup();
      const { container } = render(<ToolCard tool={baseToolCall} />);

      const header = container.querySelector('.tool-card__header');

      // Expand
      await user.click(header!);
      expect(container.querySelector('.tool-card__body')).toBeInTheDocument();

      // Collapse
      await user.click(header!);
      expect(container.querySelector('.tool-card__body')).not.toBeInTheDocument();
    });
  });

  describe('progress logs', () => {
    it('displays tool logs when expanded', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        logs: [
          {
            timestamp: Date.now(),
            message: 'Connecting to API...',
            level: 'info',
          },
          {
            timestamp: Date.now(),
            message: 'Fetching location data...',
            level: 'info',
          },
        ],
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Expand
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      // Logs should be visible
      expect(container).toHaveTextContent('Connecting to API...');
      expect(container).toHaveTextContent('Fetching location data...');
    });

    it('displays log level icons', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        logs: [
          {
            timestamp: Date.now(),
            message: 'Info message',
            level: 'info',
          },
          {
            timestamp: Date.now(),
            message: 'Warning message',
            level: 'warn',
          },
          {
            timestamp: Date.now(),
            message: 'Error message',
            level: 'error',
          },
        ],
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Expand
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      // Check log messages are present
      expect(container).toHaveTextContent('Info message');
      expect(container).toHaveTextContent('Warning message');
      expect(container).toHaveTextContent('Error message');
    });
  });

  describe('raw JSON view', () => {
    it('shows raw JSON when toggled', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        status: 'completed',
        completedAt: Date.now(),
        durationMs: 1000,
        args: { device_id: '1', include_address: true },
        result: { lat: 37.7749, lng: -122.4194, address: 'San Francisco' },
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Expand
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      // Raw JSON should not be visible initially
      expect(container.querySelector('.tool-card__raw')).not.toBeInTheDocument();

      // Toggle raw view
      const rawToggle = container.querySelector('.tool-card__raw-toggle');
      await user.click(rawToggle!);

      // Raw JSON should now be visible
      expect(container.querySelector('.tool-card__raw')).toBeInTheDocument();
      expect(container).toHaveTextContent('device_id');
      expect(container).toHaveTextContent('lat');
    });

    it('hides raw JSON when toggled off', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        status: 'completed',
        completedAt: Date.now(),
        durationMs: 1000,
        args: { test: 'data' },
        result: { result: 'data' },
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Expand and show raw
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      const rawToggle = container.querySelector('.tool-card__raw-toggle');
      await user.click(rawToggle!);

      expect(container.querySelector('.tool-card__raw')).toBeInTheDocument();

      // Hide raw
      await user.click(rawToggle!);

      expect(container.querySelector('.tool-card__raw')).not.toBeInTheDocument();
    });

    it('displays error details in raw view for failed tools', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        status: 'failed',
        completedAt: Date.now(),
        durationMs: 500,
        error: 'API timeout',
        errorDetails: { code: 'TIMEOUT', retry_after: 60 },
        args: { device_id: '1' },
      };

      const { container } = render(<ToolCard tool={tool} />);

      // Expand and show raw
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      const rawToggle = container.querySelector('.tool-card__raw-toggle');
      await user.click(rawToggle!);

      // Error details should be visible
      expect(container).toHaveTextContent('code');
      expect(container).toHaveTextContent('TIMEOUT');
    });
  });

  describe('tool icons', () => {
    it('displays correct icon for known tools', () => {
      const tools = [
        { name: 'get_current_location', icon: '📍' },
        { name: 'get_whoop_data', icon: '💓' },
        { name: 'search_notes', icon: '📝' },
        { name: 'web_search', icon: '🌐' },
        { name: 'web_fetch', icon: '🔗' },
        { name: 'spawn_worker', icon: '🤖' },
      ];

      tools.forEach(({ name, icon }) => {
        const { container, unmount } = render(
          <ToolCard tool={{ ...baseToolCall, toolName: name }} />
        );
        expect(container).toHaveTextContent(icon);
        unmount();
      });
    });

    it('displays default icon for unknown tools', () => {
      const { container } = render(
        <ToolCard tool={{ ...baseToolCall, toolName: 'unknown_tool' }} />
      );
      expect(container).toHaveTextContent('🔧');
    });
  });

  describe('CSS classes', () => {
    it('applies status-specific CSS class', () => {
      const { container, rerender } = render(
        <ToolCard tool={{ ...baseToolCall, status: 'running' }} />
      );
      expect(container.querySelector('.tool-card--running')).toBeInTheDocument();

      rerender(<ToolCard tool={{ ...baseToolCall, status: 'completed' }} />);
      expect(container.querySelector('.tool-card--completed')).toBeInTheDocument();

      rerender(<ToolCard tool={{ ...baseToolCall, status: 'failed' }} />);
      expect(container.querySelector('.tool-card--failed')).toBeInTheDocument();
    });
  });

  describe('event propagation', () => {
    it('stops propagation when clicking body elements', async () => {
      const user = userEvent.setup();
      const tool: SupervisorToolCall = {
        ...baseToolCall,
        status: 'completed',
        completedAt: Date.now(),
        durationMs: 1000,
        args: { test: 'data' },
      };

      const handleClick = vi.fn();
      const { container } = render(
        <div onClick={handleClick}>
          <ToolCard tool={tool} />
        </div>
      );

      // Expand
      const header = container.querySelector('.tool-card__header');
      await user.click(header!);

      // Click on raw toggle button - should not collapse
      const rawToggle = container.querySelector('.tool-card__raw-toggle');
      await user.click(rawToggle!);

      // Card should still be expanded (raw view visible)
      expect(container.querySelector('.tool-card__raw')).toBeInTheDocument();
    });
  });
});
