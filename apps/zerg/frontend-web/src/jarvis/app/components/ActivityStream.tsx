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

import React, { useSyncExternalStore } from 'react';
import { supervisorToolStore } from '../../lib/supervisor-tool-store';
import { ToolCard } from './ToolCard';
import './ActivityStream.css';

interface ActivityStreamProps {
  runId: number | null;
  className?: string;
}

export function ActivityStream({ runId, className }: ActivityStreamProps): React.ReactElement | null {
  // Subscribe to store updates - triggers re-render when state changes
  useSyncExternalStore(
    supervisorToolStore.subscribe.bind(supervisorToolStore),
    () => supervisorToolStore.getState()
  );

  // Filter and sort tools for this run
  const tools = runId ? supervisorToolStore.getToolsForRun(runId) : [];

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
