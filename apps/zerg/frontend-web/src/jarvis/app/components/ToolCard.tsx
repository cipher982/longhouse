/**
 * ToolCard - Displays a supervisor tool call with progressive disclosure
 *
 * Three levels of detail:
 * - Collapsed (default): icon + name + status + duration
 * - Expanded: + logs + result preview
 * - Raw: + full JSON input/output
 *
 * Design principles:
 * - Uniform treatment: same UI for all tools
 * - Information density: power users see what's happening
 * - Persistent: visible when reloading thread
 */

import React, { useState, useMemo } from 'react';
import type { SupervisorToolCall, ToolLogEntry } from '../../lib/supervisor-tool-store';
import './ToolCard.css';

interface ToolCardProps {
  tool: SupervisorToolCall;
}

// Tool name to icon mapping
const TOOL_ICONS: Record<string, string> = {
  get_current_location: '📍',
  get_whoop_data: '💓',
  search_notes: '📝',
  web_search: '🌐',
  web_fetch: '🔗',
  http_request: '📡',
  spawn_worker: '🤖',
  list_workers: '📋',
  read_worker_result: '📖',
  get_current_time: '⏰',
  send_email: '📧',
  contact_user: '💬',
  knowledge_search: '🔍',
};

function getToolIcon(toolName: string): string {
  return TOOL_ICONS[toolName] || '🔧';
}

function formatDuration(ms: number | undefined, startedAt: number): string {
  const duration = ms ?? Date.now() - startedAt;
  if (duration < 1000) {
    return `${duration}ms`;
  }
  return `${(duration / 1000).toFixed(1)}s`;
}

function getStatusIcon(status: SupervisorToolCall['status']): string {
  switch (status) {
    case 'running':
      return '⏳';
    case 'completed':
      return '✓';
    case 'failed':
      return '✗';
  }
}

function getLogLevelIcon(level: ToolLogEntry['level']): string {
  switch (level) {
    case 'debug':
      return '›';
    case 'info':
      return '›';
    case 'warn':
      return '!';
    case 'error':
      return '✗';
  }
}

export function ToolCard({ tool }: ToolCardProps): React.ReactElement {
  const [isExpanded, setIsExpanded] = useState(false);
  const [showRaw, setShowRaw] = useState(false);

  const icon = getToolIcon(tool.toolName);
  const statusIcon = getStatusIcon(tool.status);
  const duration = formatDuration(tool.durationMs, tool.startedAt);

  // Format args preview for display
  const argsDisplay = useMemo(() => {
    if (tool.argsPreview) return tool.argsPreview;
    if (tool.args) {
      const str = JSON.stringify(tool.args);
      return str.length > 60 ? str.slice(0, 57) + '...' : str;
    }
    return null;
  }, [tool.argsPreview, tool.args]);

  // Format result preview for display
  const resultDisplay = useMemo(() => {
    if (tool.error) return tool.error;
    if (tool.resultPreview) return tool.resultPreview;
    if (tool.result) {
      const str = JSON.stringify(tool.result);
      return str.length > 100 ? str.slice(0, 97) + '...' : str;
    }
    return null;
  }, [tool.resultPreview, tool.result, tool.error]);

  return (
    <div
      className={`tool-card tool-card--${tool.status}`}
      onClick={() => setIsExpanded(!isExpanded)}
    >
      {/* Header - always visible */}
      <div className="tool-card__header">
        <span className="tool-card__icon">{icon}</span>
        <span className="tool-card__name">{tool.toolName}</span>
        {argsDisplay && !isExpanded && (
          <span className="tool-card__args-preview">{argsDisplay}</span>
        )}
        <span className="tool-card__spacer" />
        <span className={`tool-card__status tool-card__status--${tool.status}`}>
          {statusIcon}
        </span>
        <span className="tool-card__duration">{duration}</span>
      </div>

      {/* Expanded content */}
      {isExpanded && (
        <div className="tool-card__body" onClick={(e) => e.stopPropagation()}>
          {/* Args (if any) */}
          {argsDisplay && (
            <div className="tool-card__section">
              <span className="tool-card__section-label">› Input:</span>
              <span className="tool-card__section-content">{argsDisplay}</span>
            </div>
          )}

          {/* Logs (if any) */}
          {tool.logs.length > 0 && (
            <div className="tool-card__logs">
              {tool.logs.map((log, i) => (
                <div
                  key={i}
                  className={`tool-card__log tool-card__log--${log.level}`}
                >
                  <span className="tool-card__log-icon">
                    {getLogLevelIcon(log.level)}
                  </span>
                  <span className="tool-card__log-message">{log.message}</span>
                </div>
              ))}
            </div>
          )}

          {/* Result/Error */}
          {resultDisplay && (
            <div className={`tool-card__section ${tool.error ? 'tool-card__section--error' : ''}`}>
              <span className="tool-card__section-label">
                {tool.error ? '✗ Error:' : '‹ Result:'}
              </span>
              <span className="tool-card__section-content">{resultDisplay}</span>
            </div>
          )}

          {/* Raw toggle */}
          <button
            className="tool-card__raw-toggle"
            onClick={(e) => {
              e.stopPropagation();
              setShowRaw(!showRaw);
            }}
          >
            {showRaw ? '▼ Hide Raw' : '▶ Show Raw'}
          </button>

          {/* Raw JSON */}
          {showRaw && (
            <div className="tool-card__raw">
              <div className="tool-card__raw-section">
                <div className="tool-card__raw-label">Input</div>
                <pre className="tool-card__raw-json">
                  {JSON.stringify(tool.args || {}, null, 2)}
                </pre>
              </div>
              <div className="tool-card__raw-section">
                <div className="tool-card__raw-label">
                  {tool.error ? 'Error' : 'Output'}
                </div>
                <pre className="tool-card__raw-json">
                  {tool.error
                    ? JSON.stringify(tool.errorDetails || { error: tool.error }, null, 2)
                    : JSON.stringify(tool.result || {}, null, 2)
                  }
                </pre>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
