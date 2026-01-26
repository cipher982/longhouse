/**
 * CommisToolCard - Displays spawn_commis tool calls with nested commis progress
 *
 * Unified component that replaces the separate CommisProgress panel.
 * Shows:
 * - Commis task description
 * - Nested tool calls within the commis
 * - Commis status (spawned/running/complete/failed)
 * - Commis summary when complete
 *
 * Two display modes:
 * - Inline: Renders in the conversation flow (default)
 * - Sticky: Lifts to top of chat when commis becomes "detached" (after DEFERRED)
 */

import React, { useState, useMemo } from 'react';
import type { ConciergeToolCall } from '../../lib/concierge-tool-store';
import {
  CheckCircleIcon,
  XCircleIcon,
  CircleIcon,
  LoaderIcon,
} from '../../../components/icons';
import { extractCommandPreview, extractExecTarget, extractExitCode, extractExecSource, extractOfflineReason } from '../../lib/tool-display';
import './CommisToolCard.css';

interface CommisToolCardProps {
  tool: ConciergeToolCall;
  isDetached?: boolean; // True when commis continues after DEFERRED
  detachedIndex?: number; // Index among detached commis for stacking offset
}

interface NestedToolCall {
  toolCallId: string;
  toolName: string;
  status: 'running' | 'completed' | 'failed';
  argsPreview?: string;
  resultPreview?: string;
  error?: string;
  startedAt: number;
  durationMs?: number;
}

interface CommisState {
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
 * Commis status icon
 */
function CommisStatusIcon({ status }: { status: CommisState['status'] }) {
  switch (status) {
    case 'spawned':
      return <span className="commis-icon-spawned"><CircleIcon width={14} height={14} /></span>;
    case 'running':
      return <span className="commis-icon-running"><LoaderIcon width={14} height={14} /></span>;
    case 'complete':
      return <span className="commis-icon-complete"><CheckCircleIcon width={14} height={14} /></span>;
    case 'failed':
      return <span className="commis-icon-failed"><XCircleIcon width={14} height={14} /></span>;
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

interface NestedToolItemProps {
  tool: NestedToolCall;
  isExpanded: boolean;
  isCompact: boolean;
  onToggleExpand: () => void;
}

/**
 * Render a single nested tool call with expandable details
 */
function NestedToolItem({ tool, isExpanded, isCompact, onToggleExpand }: NestedToolItemProps) {
  const statusClass = `nested-tool-status-${tool.status}`;
  const duration = getElapsedTime(tool.startedAt, tool.status, tool.durationMs);
  const command = extractCommandPreview(tool.toolName, tool.argsPreview);
  const target = extractExecTarget(tool.toolName, tool.argsPreview);
  const exitCode = extractExitCode(tool.resultPreview, tool.error);
  const execSource = extractExecSource(tool.toolName);
  const offlineReason = extractOfflineReason(tool.error, tool.resultPreview);
  const primaryLabel = command ?? tool.toolName;

  // Show args preview if running, error preview if failed
  let preview = '';
  if (tool.status === 'running' && tool.argsPreview && !command) {
    preview = truncatePreview(tool.argsPreview, 50);
  } else if (tool.status === 'failed' && tool.error) {
    preview = truncatePreview(tool.error, 50);
  } else if (tool.status === 'completed' && tool.resultPreview) {
    preview = truncatePreview(tool.resultPreview, 70);
  }

  const metaItems: Array<{ label: string; className?: string }> = [];
  if (command) {
    metaItems.push({ label: tool.toolName, className: 'nested-tool-meta-item' });
  }
  if (execSource) {
    metaItems.push({ label: execSource, className: 'nested-tool-meta-item nested-tool-meta-item--source' });
  }
  if (target) {
    metaItems.push({ label: `target: ${target}`, className: 'nested-tool-meta-item' });
  }
  if (offlineReason) {
    metaItems.push({ label: offlineReason, className: 'nested-tool-meta-item nested-tool-meta-item--offline' });
  }
  if (exitCode !== null) {
    const exitClass = exitCode === 0 ? 'nested-tool-meta-item nested-tool-meta-item--ok' : 'nested-tool-meta-item nested-tool-meta-item--warn';
    metaItems.push({ label: `exit ${exitCode}`, className: exitClass });
  }

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (command && navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(command).catch(() => {
        // Silently fail - clipboard may not be available in insecure contexts
      });
    }
  };

  return (
    <div className={`nested-tool ${statusClass}`}>
      <div className="nested-tool-row" onClick={onToggleExpand}>
        <span className="nested-tool-expand-indicator">{isExpanded ? '▼' : '▶'}</span>
        <span className="nested-tool-icon">
          <ToolStatusIcon status={tool.status} />
        </span>
        <span className={`nested-tool-name${command ? ' nested-tool-name--command' : ''}`}>{primaryLabel}</span>
        {metaItems.length > 0 && (
          <span className="nested-tool-meta">
            {metaItems.map((item, index) => (
              <span key={`${tool.toolCallId}-meta-${index}`} className={item.className}>{item.label}</span>
            ))}
          </span>
        )}
        {!isCompact && preview && <span className="nested-tool-preview">{preview}</span>}
        {command && (
          <button
            className="nested-tool-copy"
            onClick={handleCopy}
            title="Copy command"
          >
            📋
          </button>
        )}
        <span className="nested-tool-duration">{duration}</span>
      </div>

      {/* Expandable details drawer */}
      {isExpanded && (
        <div className="nested-tool-details" data-testid="nested-tool-details">
          {tool.argsPreview && (
            <div className="nested-tool-details__section">
              <span className="nested-tool-details__label">Args</span>
              <pre className="nested-tool-details__content">{tool.argsPreview}</pre>
            </div>
          )}
          {tool.resultPreview && (
            <div className="nested-tool-details__section">
              <span className="nested-tool-details__label">Result</span>
              <pre className="nested-tool-details__content">{tool.resultPreview}</pre>
            </div>
          )}
          {tool.error && (
            <div className="nested-tool-details__section nested-tool-details__section--error">
              <span className="nested-tool-details__label">Error</span>
              <pre className="nested-tool-details__content">{tool.error}</pre>
            </div>
          )}
          {!tool.argsPreview && !tool.resultPreview && !tool.error && (
            <div className="nested-tool-details__section">
              <span className="nested-tool-details__content nested-tool-details__content--empty">No details available</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function CommisToolCard({ tool, isDetached = false, detachedIndex = 0 }: CommisToolCardProps): React.ReactElement {
  const [isExpanded, setIsExpanded] = useState(true); // Commiss default to expanded
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [isCompact, setIsCompact] = useState(false);

  const toggleToolExpand = (toolCallId: string) => {
    setExpandedTools(prev => {
      const next = new Set(prev);
      if (next.has(toolCallId)) {
        next.delete(toolCallId);
      } else {
        next.add(toolCallId);
      }
      return next;
    });
  };

  // Extract commis state from tool metadata
  const commisState = useMemo<CommisState>(() => {
    // Commis state is stored in tool.result as metadata
    const metadata = tool.result as any;
    return {
      status: metadata?.commisStatus || (tool.status === 'running' ? 'running' : tool.status === 'failed' ? 'failed' : 'complete'),
      summary: metadata?.commisSummary,
      nestedTools: metadata?.nestedTools || [],
    };
  }, [tool]);

  const taskDisplay = useMemo(() => {
    // Extract task from args
    const task = (tool.args as any)?.task || 'Commis task';
    return task.length > 60 ? task.slice(0, 57) + '...' : task;
  }, [tool.args]);

  const duration = formatDuration(tool.durationMs, tool.startedAt, commisState.status);
  const hasNestedTools = commisState.nestedTools.length > 0;

  const containerClass = `commis-tool-card commis-tool-card--${commisState.status} ${isDetached ? 'commis-tool-card--detached' : ''} ${isCompact ? 'commis-tool-card--compact' : ''}`;

  // Compute stacking offset for detached commis
  const detachedStyle = isDetached && detachedIndex > 0 ? {
    top: `${detachedIndex * 8}px`, // Stack with 8px vertical offset
  } : undefined;

  return (
    <div
      className={containerClass}
      style={detachedStyle}
      data-testid="commis-tool-card"
      data-tool-call-id={tool.toolCallId}
    >
      {/* Header - always visible */}
      <div
        className="commis-tool-card__header"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <span className="commis-tool-card__icon">
          <CommisStatusIcon status={commisState.status} />
        </span>
        <span className="commis-tool-card__name">spawn_commis</span>
        <span className="commis-tool-card__task">{taskDisplay}</span>
        <span className="commis-tool-card__spacer" />
        {hasNestedTools && (
          <button
            className="commis-tool-card__compact-toggle"
            onClick={(e) => { e.stopPropagation(); setIsCompact(!isCompact); }}
            title={isCompact ? 'Expand rows' : 'Compact rows'}
          >
            {isCompact ? '⊞' : '⊟'}
          </button>
        )}
        <span className="commis-tool-card__duration">{duration}</span>
        <span className="commis-tool-card__expand-toggle">
          {isExpanded ? '▼' : '▶'}
        </span>
      </div>

      {/* Expanded content - nested tools and summary */}
      {isExpanded && (
        <div className="commis-tool-card__body" onClick={(e) => e.stopPropagation()}>
          {/* Nested tool calls */}
          {hasNestedTools && (
            <div className="commis-tool-card__nested-tools">
              {commisState.nestedTools.map(nestedTool => (
                <NestedToolItem
                  key={nestedTool.toolCallId}
                  tool={nestedTool}
                  isExpanded={expandedTools.has(nestedTool.toolCallId)}
                  isCompact={isCompact}
                  onToggleExpand={() => toggleToolExpand(nestedTool.toolCallId)}
                />
              ))}
            </div>
          )}

          {/* Commis summary */}
          {commisState.summary && (
            <div className="commis-tool-card__summary">
              <span className="commis-tool-card__summary-label">Summary:</span>
              <span className="commis-tool-card__summary-text">{commisState.summary}</span>
            </div>
          )}

          {/* Error display */}
          {tool.error && (
            <div className="commis-tool-card__error">
              <span className="commis-tool-card__error-label">✗ Error:</span>
              <span className="commis-tool-card__error-text">{tool.error}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
