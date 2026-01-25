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
import { WorkerToolCard } from './WorkerToolCard';
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
  const tools = runId != null ? supervisorToolStore.getToolsForRun(runId) : [];

  // Don't render if no tools
  if (tools.length === 0) {
    return null;
  }

  const hasActiveWork = tools.some(t => {
    if (t.status === 'running') return true;
    if (t.toolName !== 'spawn_commis') return false;

    const workerStatus = (t.result as any)?.workerStatus;
    const nestedTools = (t.result as any)?.nestedTools || [];

    if (workerStatus === 'spawned' || workerStatus === 'running') return true;
    if (nestedTools.some((nt: any) => nt.status === 'running')) return true;

    return false;
  });

  // Check if supervisor is deferred (workers continuing in background)
  const isDeferred = supervisorToolStore.isDeferred(runId);

  // Count detached workers before this one for stacking offset
  let detachedWorkerIndex = 0;

  return (
    <div className={`activity-stream ${className || ''} ${hasActiveWork ? 'activity-stream--active' : ''}`}>
      {tools.map(tool => {
        // Use WorkerToolCard for spawn_commis, regular ToolCard for everything else
        if (tool.toolName === 'spawn_commis') {
          // Mark worker as detached if it's still running while supervisor is deferred
          const workerStatus = (tool.result as any)?.workerStatus;
          const isDetached = isDeferred && (workerStatus === 'running' || workerStatus === 'spawned');
          const detachedIndex = isDetached ? detachedWorkerIndex++ : 0;
          return <WorkerToolCard key={tool.toolCallId} tool={tool} isDetached={isDetached} detachedIndex={detachedIndex} />;
        }
        return <ToolCard key={tool.toolCallId} tool={tool} />;
      })}
    </div>
  );
}
