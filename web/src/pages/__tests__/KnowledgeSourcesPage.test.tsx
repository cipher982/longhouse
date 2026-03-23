import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import KnowledgeSourcesPage from '../KnowledgeSourcesPage';

const mockCreateMutation = {
  mutateAsync: vi.fn().mockResolvedValue({ id: 1 }),
};

const mockReposData = {
  repositories: [] as Array<unknown>,
  page: 1,
  per_page: 30,
  has_more: false,
};

const mockBranchesData = {
  branches: [] as Array<unknown>,
};

vi.mock('../../hooks/useKnowledgeSources', () => ({
  useKnowledgeSources: () => ({ data: [], isLoading: false, error: null }),
  useDeleteKnowledgeSource: () => ({ mutate: vi.fn() }),
  useSyncKnowledgeSource: () => ({ mutateAsync: vi.fn() }),
  useCreateKnowledgeSource: () => mockCreateMutation,
  useGitHubRepos: () => ({
    data: mockReposData,
    isLoading: false,
    error: null,
  }),
  useGitHubBranches: () => ({
    data: mockBranchesData,
    isLoading: false,
    error: null,
  }),
}));

vi.mock('../../components/confirm', () => ({
  useConfirm: () => () => Promise.resolve(true),
}));

describe('KnowledgeSourcesPage', () => {
  beforeEach(() => {
    mockCreateMutation.mutateAsync.mockClear();
  });

  it('creates a user_text knowledge source from Add Context modal', async () => {
    render(<KnowledgeSourcesPage />);

    fireEvent.click(screen.getByTestId('add-context-btn'));

    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: 'My Notes' },
    });
    fireEvent.change(screen.getByLabelText('Content'), {
      target: { value: 'Important context' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Document' }));

    await waitFor(() => {
      expect(mockCreateMutation.mutateAsync).toHaveBeenCalledTimes(1);
    });

    expect(mockCreateMutation.mutateAsync).toHaveBeenCalledWith({
      name: 'My Notes',
      source_type: 'user_text',
      config: { content: 'Important context' },
    });
  });
});
