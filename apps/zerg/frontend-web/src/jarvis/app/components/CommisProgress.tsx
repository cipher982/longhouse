/**
 * Commis Progress Component
 *
 * Shows live progress when the Concierge delegates tasks to commis.
 * Only displays when commis are actively running - does NOT show a
 * "thinking" indicator (that's handled by the assistant message bubble).
 *
 * Shows:
 * - Commis spawn/start/complete status
 * - Live tool call activity within commis
 * - Commis summaries when complete
 */

import { useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { commisProgressStore, type CommisState, type ToolCall } from '../../lib/commis-progress-store';
import { extractCommandPreview } from '../../lib/tool-display';
import {
  CheckCircleIcon,
  XCircleIcon,
  CircleIcon,
  CircleDotIcon,
  LoaderIcon,
  PlayIcon,
} from '../../../components/icons';

/**
 * Display mode for commis progress UI
 */
type DisplayMode = 'floating' | 'inline' | 'sticky';

interface CommisProgressProps {
  mode?: DisplayMode;
}

/**
 * Get elapsed time since start (only for running tools)
 */
function getElapsedTime(startedAt: number, status: string, durationMs?: number): string {
  // Use captured duration for completed/failed tools to avoid "ticking" on re-render
  let elapsed: number;
  if (durationMs !== undefined) {
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

/**
 * Truncate preview text
 */
function truncatePreview(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return text.substring(0, maxLen - 3) + '...';
}

/**
 * Tool status icon
 */
function ToolStatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'running':
      return <span className="icon-running"><CircleIcon width={12} height={12} /></span>;
    case 'completed':
      return <span className="icon-completed"><CheckCircleIcon width={12} height={12} /></span>;
    case 'failed':
      return <span className="icon-failed"><XCircleIcon width={12} height={12} /></span>;
    default:
      return <span className="icon-default"><CircleDotIcon width={12} height={12} /></span>;
  }
}

/**
 * Commis status icon
 */
function CommisStatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'spawned':
      return <span className="icon-spawned"><CircleIcon width={14} height={14} /></span>;
    case 'running':
      return <span className="icon-commis-running"><LoaderIcon width={14} height={14} /></span>;
    case 'complete':
      return <span className="icon-complete"><CheckCircleIcon width={14} height={14} /></span>;
    case 'failed':
      return <span className="icon-commis-failed"><XCircleIcon width={14} height={14} /></span>;
    default:
      return <span className="icon-queued"><PlayIcon width={14} height={14} /></span>;
  }
}

/**
 * Render a single tool call
 */
function ToolCallItem({ tool }: { tool: ToolCall }) {
  const statusClass = `tool-status-${tool.status}`;
  const duration = getElapsedTime(tool.startedAt, tool.status, tool.durationMs);
  const command = extractCommandPreview(tool.toolName, tool.argsPreview);
  const primaryLabel = command ?? tool.toolName;

  // Show args preview if running, result/error preview if done
  let preview = '';
  if (tool.status === 'running' && tool.argsPreview && !command) {
    preview = truncatePreview(tool.argsPreview, 50);
  } else if (tool.status === 'failed' && tool.error) {
    preview = truncatePreview(tool.error, 50);
  }

  return (
    <div className={`commis-tool ${statusClass}`}>
      <span className="tool-icon">
        <ToolStatusIcon status={tool.status} />
      </span>
      <span className={`tool-name${command ? ' tool-name--command' : ''}`}>{primaryLabel}</span>
      {command && <span className="tool-meta">{tool.toolName}</span>}
      {preview && <span className="tool-preview">{preview}</span>}
      <span className="tool-duration">{duration}</span>
    </div>
  );
}

/**
 * Render a single commis status
 */
function CommisItem({ commis }: { commis: CommisState }) {
  const statusClass = `commis-status-${commis.status}`;
  const taskPreview = commis.task.length > 40
    ? commis.task.substring(0, 40) + '...'
    : commis.task;

  const toolCallsArray = Array.from(commis.toolCalls.values());
  const hasToolCalls = toolCallsArray.length > 0;

  return (
    <div className={`concierge-commis ${statusClass}`}>
      <div className="commis-header">
        <span className="commis-icon">
          <CommisStatusIcon status={commis.status} />
        </span>
        <span className="commis-task">{taskPreview}</span>
      </div>
      {hasToolCalls && (
        <div className="commis-tools">
          {toolCallsArray.map(tool => (
            <ToolCallItem key={tool.toolCallId} tool={tool} />
          ))}
        </div>
      )}
      {commis.summary && (
        <div className="commis-summary">{commis.summary}</div>
      )}
    </div>
  );
}

/**
 * Commis Progress UI Component
 */
export function CommisProgress({ mode = 'sticky' }: CommisProgressProps) {
  // Subscribe to external store
  const state = useSyncExternalStore(
    commisProgressStore.subscribe.bind(commisProgressStore),
    () => commisProgressStore.getState()
  );

  if (!state.isActive) {
    return null;
  }

  const commisArray = Array.from(state.commis.values());
  const runningCommis = commisArray.filter(w => w.status === 'running' || w.status === 'spawned');

  const modeClass = mode === 'floating' ? 'commis-progress--floating' : mode === 'sticky' ? 'commis-progress--sticky' : '';

  // Determine status label
  const statusLabel = state.reconnecting
    ? 'Reconnecting to active task...'
    : 'Investigating...';

  const content = (
    <div className={`commis-progress commis-progress--active ${modeClass}`}>
      <div className="commis-progress-content">
        <div className="concierge-status">
          <div className="concierge-spinner"></div>
          <span className="concierge-label">{statusLabel}</span>
        </div>
        {commisArray.length > 0 && (
          <div className="concierge-commis">
            {commisArray.map(commis => (
              <CommisItem key={commis.jobId} commis={commis} />
            ))}
          </div>
        )}
        {runningCommis.length > 0 && (
          <div className="concierge-active-count">
            {runningCommis.length} commis running...
          </div>
        )}
      </div>
    </div>
  );

  // For sticky mode, render inline (inside chat container)
  // For floating mode, use portal to render at body level
  if (mode === 'floating') {
    return createPortal(content, document.body);
  }

  // For sticky/inline mode, render where component is placed
  return content;
}
