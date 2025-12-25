/**
 * Worker Progress Component
 *
 * Shows live progress when the Supervisor delegates tasks to workers.
 * Only displays when workers are actively running - does NOT show a
 * "thinking" indicator (that's handled by the assistant message bubble).
 *
 * Shows:
 * - Worker spawn/start/complete status
 * - Live tool call activity within workers
 * - Worker summaries when complete
 */

import { useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { workerProgressStore, type WorkerState, type ToolCall } from '../../lib/worker-progress-store';
import {
  CheckCircleIcon,
  XCircleIcon,
  CircleIcon,
  CircleDotIcon,
  LoaderIcon,
  PlayIcon,
} from '../../../components/icons';

/**
 * Display mode for worker progress UI
 */
type DisplayMode = 'floating' | 'inline' | 'sticky';

interface WorkerProgressProps {
  mode?: DisplayMode;
}

/**
 * Get elapsed time since start
 */
function getElapsedTime(startedAt: number): string {
  const elapsed = Date.now() - startedAt;
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
 * Worker status icon
 */
function WorkerStatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'spawned':
      return <span className="icon-spawned"><CircleIcon width={14} height={14} /></span>;
    case 'running':
      return <span className="icon-worker-running"><LoaderIcon width={14} height={14} /></span>;
    case 'complete':
      return <span className="icon-complete"><CheckCircleIcon width={14} height={14} /></span>;
    case 'failed':
      return <span className="icon-worker-failed"><XCircleIcon width={14} height={14} /></span>;
    default:
      return <span className="icon-queued"><PlayIcon width={14} height={14} /></span>;
  }
}

/**
 * Render a single tool call
 */
function ToolCallItem({ tool }: { tool: ToolCall }) {
  const statusClass = `tool-status-${tool.status}`;
  const duration = tool.durationMs ? `${tool.durationMs}ms` : getElapsedTime(tool.startedAt);

  // Show args preview if running, result/error preview if done
  let preview = '';
  if (tool.status === 'running' && tool.argsPreview) {
    preview = truncatePreview(tool.argsPreview, 50);
  } else if (tool.status === 'failed' && tool.error) {
    preview = truncatePreview(tool.error, 50);
  }

  return (
    <div className={`worker-tool ${statusClass}`}>
      <span className="tool-icon">
        <ToolStatusIcon status={tool.status} />
      </span>
      <span className="tool-name">{tool.toolName}</span>
      {preview && <span className="tool-preview">{preview}</span>}
      <span className="tool-duration">{duration}</span>
    </div>
  );
}

/**
 * Render a single worker status
 */
function WorkerItem({ worker }: { worker: WorkerState }) {
  const statusClass = `worker-status-${worker.status}`;
  const taskPreview = worker.task.length > 40
    ? worker.task.substring(0, 40) + '...'
    : worker.task;

  const toolCallsArray = Array.from(worker.toolCalls.values());
  const hasToolCalls = toolCallsArray.length > 0;

  return (
    <div className={`supervisor-worker ${statusClass}`}>
      <div className="worker-header">
        <span className="worker-icon">
          <WorkerStatusIcon status={worker.status} />
        </span>
        <span className="worker-task">{taskPreview}</span>
      </div>
      {hasToolCalls && (
        <div className="worker-tools">
          {toolCallsArray.map(tool => (
            <ToolCallItem key={tool.toolCallId} tool={tool} />
          ))}
        </div>
      )}
      {worker.summary && (
        <div className="worker-summary">{worker.summary}</div>
      )}
    </div>
  );
}

/**
 * Worker Progress UI Component
 */
export function WorkerProgress({ mode = 'sticky' }: WorkerProgressProps) {
  // Subscribe to external store
  const state = useSyncExternalStore(
    workerProgressStore.subscribe.bind(workerProgressStore),
    () => workerProgressStore.getState()
  );

  if (!state.isActive) {
    return null;
  }

  const workersArray = Array.from(state.workers.values());
  const runningWorkers = workersArray.filter(w => w.status === 'running' || w.status === 'spawned');

  const modeClass = mode === 'floating' ? 'worker-progress--floating' : mode === 'sticky' ? 'worker-progress--sticky' : '';

  const content = (
    <div className={`worker-progress worker-progress--active ${modeClass}`}>
      <div className="worker-progress-content">
        <div className="supervisor-status">
          <div className="supervisor-spinner"></div>
          <span className="supervisor-label">Investigating...</span>
        </div>
        {workersArray.length > 0 && (
          <div className="supervisor-workers">
            {workersArray.map(worker => (
              <WorkerItem key={worker.jobId} worker={worker} />
            ))}
          </div>
        )}
        {runningWorkers.length > 0 && (
          <div className="supervisor-active-count">
            {runningWorkers.length} worker{runningWorkers.length > 1 ? 's' : ''} running...
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
