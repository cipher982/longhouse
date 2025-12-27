/**
 * ActivityStream - Displays supervisor tool calls inline in conversation
 *
 * Shows tool cards between user message and assistant response.
 * Provides real-time updates during execution and persists after completion.
 *
 * Usage:
 *   <ActivityStream runId={currentRunId} />
 *
 * Design: Stack of ToolCards, ordered by start time
 */

import React, { useSyncExternalStore, useMemo } from 'react';
import { supervisorToolStore } from '../../lib/supervisor-tool-store';
import { ToolCard } from './ToolCard';
import './ActivityStream.css';

interface ActivityStreamProps {
  runId: number | null;
  className?: string;
}

export function ActivityStream({ runId, className }: ActivityStreamProps): React.ReactElement | null {
  // Subscribe to store updates
  const state = useSyncExternalStore(
    supervisorToolStore.subscribe.bind(supervisorToolStore),
    () => supervisorToolStore.getState()
  );

  // Filter and sort tools for this run
  const tools = useMemo(() => {
    if (!runId) return [];
    return supervisorToolStore.getToolsForRun(runId);
  }, [runId, state]);

  // Don't render if no tools
  if (tools.length === 0) {
    return null;
  }

  const hasRunningTools = tools.some(t => t.status === 'running');

  return (
    <div className={`activity-stream ${className || ''} ${hasRunningTools ? 'activity-stream--active' : ''}`}>
      {tools.map(tool => (
        <ToolCard key={tool.toolCallId} tool={tool} />
      ))}
    </div>
  );
}

/**
 * Hook to check if there are any tools for a run
 */
function useHasToolsForRunImpl(runId: number | null): boolean {
  const state = useSyncExternalStore(
    supervisorToolStore.subscribe.bind(supervisorToolStore),
    () => supervisorToolStore.getState()
  );

  return useMemo(() => {
    if (!runId) return false;
    return supervisorToolStore.getToolsForRun(runId).length > 0;
  }, [runId, state]);
}

export { useHasToolsForRunImpl as useHasToolsForRun };
