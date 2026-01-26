/**
 * ActivityStream - Displays concierge tool calls inline in conversation
 *
 * Shows tool cards between user message and assistant response.
 * Provides real-time updates during execution and persists after completion.
 *
 * Usage:
 *   <ActivityStream courseId={currentCourseId} />
 *
 * Design: Stack of ToolCards, ordered by start time
 */

import React, { useSyncExternalStore } from 'react';
import { conciergeToolStore } from '../../lib/concierge-tool-store';
import { ToolCard } from './ToolCard';
import { CommisToolCard } from './CommisToolCard';
import './ActivityStream.css';

interface ActivityStreamProps {
  courseId: number | null;
  className?: string;
}

export function ActivityStream({ courseId, className }: ActivityStreamProps): React.ReactElement | null {
  // Subscribe to store updates - triggers re-render when state changes
  useSyncExternalStore(
    conciergeToolStore.subscribe.bind(conciergeToolStore),
    () => conciergeToolStore.getState()
  );

  // Filter and sort tools for this course
  const tools = courseId != null ? conciergeToolStore.getToolsForCourse(courseId) : [];

  // Don't render if no tools
  if (tools.length === 0) {
    return null;
  }

  const hasActiveWork = tools.some(t => {
    if (t.status === 'running') return true;
    if (t.toolName !== 'spawn_commis') return false;

    const commisStatus = (t.result as any)?.commisStatus;
    const nestedTools = (t.result as any)?.nestedTools || [];

    if (commisStatus === 'spawned' || commisStatus === 'running') return true;
    if (nestedTools.some((nt: any) => nt.status === 'running')) return true;

    return false;
  });

  // Check if concierge is deferred (commis continuing in background)
  const isDeferred = conciergeToolStore.isDeferred(courseId);

  // Count detached commis before this one for stacking offset
  let detachedCommisIndex = 0;

  return (
    <div className={`activity-stream ${className || ''} ${hasActiveWork ? 'activity-stream--active' : ''}`}>
      {tools.map(tool => {
        // Use CommisToolCard for spawn_commis, regular ToolCard for everything else
        if (tool.toolName === 'spawn_commis') {
          // Mark commis as detached if it's still running while concierge is deferred
          const commisStatus = (tool.result as any)?.commisStatus;
          const isDetached = isDeferred && (commisStatus === 'running' || commisStatus === 'spawned');
          const detachedIndex = isDetached ? detachedCommisIndex++ : 0;
          return <CommisToolCard key={tool.toolCallId} tool={tool} isDetached={isDetached} detachedIndex={detachedIndex} />;
        }
        return <ToolCard key={tool.toolCallId} tool={tool} />;
      })}
    </div>
  );
}
