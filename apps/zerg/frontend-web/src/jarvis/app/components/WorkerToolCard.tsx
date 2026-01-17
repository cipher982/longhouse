/**
 * WorkerToolCard - Displays spawn_worker tool calls with nested worker progress
 *
 * Unified component that replaces the separate WorkerProgress panel.
 * Shows:
 * - Worker task description
 * - Nested tool calls within the worker
 * - Worker status (spawned/running/complete/failed)
 * - Worker summary when complete
 *
 * Two display modes:
 * - Inline: Renders in the conversation flow (default)
 * - Sticky: Lifts to top of chat when worker becomes "detached" (after DEFERRED)
 */

import React, { useState, useMemo } from 'react';
import type { SupervisorToolCall } from '../../lib/supervisor-tool-store';
import {
  CheckCircleIcon,
  XCircleIcon,
  CircleIcon,
  LoaderIcon,
} from '../../../components/icons';
import { extractCommandPreview } from '../../lib/tool-display';
import './WorkerToolCard.css';

interface WorkerToolCardProps {
  tool: SupervisorToolCall;
  isDetached?: boolean; // True when worker continues after DEFERRED
  detachedIndex?: number; // Index among detached workers for stacking offset
}

interface NestedToolCall {
  toolCallId: string;
  toolName: string;
  status: 'running' | 'completed' | 'failed';
  argsPreview?: string;
  error?: string;
  startedAt: number;
  durationMs?: number;
}

interface WorkerState {
  status: 'spawned' | 'running' | 'complete' | 'failed';
  summary?: string;
  nestedTools: NestedToolCall[];
}

function formatDuration(ms: number | undefined, startedAt: number, status: string): string {
  // Only use live calculation (Date.now()) for running tools
  // Completed/failed tools should have durationMs set; if not, show "—" to avoid ticking
  let duration: number;
  if (ms != null) {
    duration = ms;
  } else if (status === 'running' || status === 'spawned') {
    duration = Date.now() - startedAt;
  } else {
    return '—';
  }

  if (duration < 1000) {
    return `${duration}ms`;
  }
  return `${(duration / 1000).toFixed(1)}s`;
}

function getElapsedTime(startedAt: number, status: string, durationMs?: number): string {
  // Only use live calculation for running tools
  let elapsed: number;
  if (durationMs != null) {
    elapsed = durationMs;
  } else if (status === 'running') {
    elapsed = Date.now() - startedAt;
  } else {
    return '—';
  }

  if (elapsed < 1000) {
    return `${elapsed}ms`;
  }
  return `${(elapsed / 1000).toFixed(1)}s`;
}

function truncatePreview(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return text.substring(0, maxLen - 3) + '...';
}

/**
 * Worker status icon
 */
function WorkerStatusIcon({ status }: { status: WorkerState['status'] }) {
  switch (status) {
    case 'spawned':
      return <span className="worker-icon-spawned"><CircleIcon width={14} height={14} /></span>;
    case 'running':
      return <span className="worker-icon-running"><LoaderIcon width={14} height={14} /></span>;
    case 'complete':
      return <span className="worker-icon-complete"><CheckCircleIcon width={14} height={14} /></span>;
    case 'failed':
      return <span className="worker-icon-failed"><XCircleIcon width={14} height={14} /></span>;
  }
}

/**
 * Tool status icon (for nested tools)
 */
function ToolStatusIcon({ status }: { status: NestedToolCall['status'] }) {
  switch (status) {
    case 'running':
      return <span className="tool-icon-running"><CircleIcon width={12} height={12} /></span>;
    case 'completed':
      return <span className="tool-icon-completed"><CheckCircleIcon width={12} height={12} /></span>;
    case 'failed':
      return <span className="tool-icon-failed"><XCircleIcon width={12} height={12} /></span>;
  }
}

/**
 * Render a single nested tool call
 */
function NestedToolItem({ tool }: { tool: NestedToolCall }) {
  const statusClass = `nested-tool-status-${tool.status}`;
  const duration = getElapsedTime(tool.startedAt, tool.status, tool.durationMs);
  const command = extractCommandPreview(tool.toolName, tool.argsPreview);
  const primaryLabel = command ?? tool.toolName;

  // Show args preview if running, error preview if failed
  let preview = '';
  if (tool.status === 'running' && tool.argsPreview && !command) {
    preview = truncatePreview(tool.argsPreview, 50);
  } else if (tool.status === 'failed' && tool.error) {
    preview = truncatePreview(tool.error, 50);
  }

  return (
    <div className={`nested-tool ${statusClass}`}>
      <span className="nested-tool-icon">
        <ToolStatusIcon status={tool.status} />
      </span>
      <span className={`nested-tool-name${command ? ' nested-tool-name--command' : ''}`}>{primaryLabel}</span>
      {command && <span className="nested-tool-meta">{tool.toolName}</span>}
      {preview && <span className="nested-tool-preview">{preview}</span>}
      <span className="nested-tool-duration">{duration}</span>
    </div>
  );
}

export function WorkerToolCard({ tool, isDetached = false, detachedIndex = 0 }: WorkerToolCardProps): React.ReactElement {
  const [isExpanded, setIsExpanded] = useState(true); // Workers default to expanded

  // Extract worker state from tool metadata
  const workerState = useMemo<WorkerState>(() => {
    // Worker state is stored in tool.result as metadata
    const metadata = tool.result as any;
    return {
      status: metadata?.workerStatus || (tool.status === 'running' ? 'running' : tool.status === 'failed' ? 'failed' : 'complete'),
      summary: metadata?.workerSummary,
      nestedTools: metadata?.nestedTools || [],
    };
  }, [tool]);

  const taskDisplay = useMemo(() => {
    // Extract task from args
    const task = (tool.args as any)?.task || 'Worker task';
    return task.length > 60 ? task.slice(0, 57) + '...' : task;
  }, [tool.args]);

  const duration = formatDuration(tool.durationMs, tool.startedAt, workerState.status);
  const hasNestedTools = workerState.nestedTools.length > 0;

  const containerClass = `worker-tool-card worker-tool-card--${workerState.status} ${isDetached ? 'worker-tool-card--detached' : ''}`;

  // Compute stacking offset for detached workers
  const detachedStyle = isDetached && detachedIndex > 0 ? {
    top: `${detachedIndex * 8}px`, // Stack with 8px vertical offset
  } : undefined;

  return (
    <div
      className={containerClass}
      style={detachedStyle}
      data-testid="worker-tool-card"
      data-tool-call-id={tool.toolCallId}
    >
      {/* Header - always visible */}
      <div
        className="worker-tool-card__header"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <span className="worker-tool-card__icon">
          <WorkerStatusIcon status={workerState.status} />
        </span>
        <span className="worker-tool-card__name">spawn_worker</span>
        <span className="worker-tool-card__task">{taskDisplay}</span>
        <span className="worker-tool-card__spacer" />
        <span className="worker-tool-card__duration">{duration}</span>
        <span className="worker-tool-card__expand-toggle">
          {isExpanded ? '▼' : '▶'}
        </span>
      </div>

      {/* Expanded content - nested tools and summary */}
      {isExpanded && (
        <div className="worker-tool-card__body" onClick={(e) => e.stopPropagation()}>
          {/* Nested tool calls */}
          {hasNestedTools && (
            <div className="worker-tool-card__nested-tools">
              {workerState.nestedTools.map(nestedTool => (
                <NestedToolItem key={nestedTool.toolCallId} tool={nestedTool} />
              ))}
            </div>
          )}

          {/* Worker summary */}
          {workerState.summary && (
            <div className="worker-tool-card__summary">
              <span className="worker-tool-card__summary-label">Summary:</span>
              <span className="worker-tool-card__summary-text">{workerState.summary}</span>
            </div>
          )}

          {/* Error display */}
          {tool.error && (
            <div className="worker-tool-card__error">
              <span className="worker-tool-card__error-label">✗ Error:</span>
              <span className="worker-tool-card__error-text">{tool.error}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
