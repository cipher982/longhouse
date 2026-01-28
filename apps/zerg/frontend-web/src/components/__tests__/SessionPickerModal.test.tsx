/**
 * SessionPickerModal Tests
 *
 * Tests for the session picker modal, focusing on:
 * - Null/undefined filter handling (regression test for crash)
 */

import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { SessionPickerModal } from '../SessionPickerModal';

// Mock the API calls - use hoisted pattern for proper module mocking
const apiMocks = vi.hoisted(() => ({
  fetchSessions: vi.fn().mockResolvedValue({ sessions: [] }),
  fetchSessionPreview: vi.fn().mockResolvedValue({ messages: [], total_messages: 0 }),
}));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    ...apiMocks,
  };
});

function renderModal(props: Partial<React.ComponentProps<typeof SessionPickerModal>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  const defaultProps = {
    isOpen: true,
    onClose: vi.fn(),
    onSelect: vi.fn(),
    ...props,
  };

  return render(
    <QueryClientProvider client={queryClient}>
      <SessionPickerModal {...defaultProps} />
    </QueryClientProvider>
  );
}

describe('SessionPickerModal', () => {
  describe('Filter handling', () => {
    it('handles undefined initialFilters without crashing', () => {
      // This should not throw
      expect(() => renderModal({ initialFilters: undefined })).not.toThrow();
    });

    it('handles null initialFilters without crashing (regression)', () => {
      // This was the bug: null filters caused "Cannot read properties of null (reading 'query')"
      // The backend can send filters: null which flows through the SSE handler
      expect(() => renderModal({ initialFilters: null as any })).not.toThrow();
    });

    it('handles empty object initialFilters', () => {
      expect(() => renderModal({ initialFilters: {} })).not.toThrow();
    });

    it('handles filters with values', () => {
      expect(() => renderModal({
        initialFilters: {
          project: 'zerg',
          query: 'test',
          provider: 'claude',
        },
      })).not.toThrow();
    });
  });

  describe('Rendering', () => {
    it('does not render when isOpen is false', () => {
      const { container } = renderModal({ isOpen: false });
      expect(container.querySelector('.session-picker-modal')).toBeNull();
    });

    it('renders when isOpen is true', () => {
      const { container } = renderModal({ isOpen: true });
      expect(container.querySelector('.session-picker-modal')).not.toBeNull();
    });
  });
});
