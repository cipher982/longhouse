import { useEffect, useRef } from 'react';
import '../styles/execution-log-stream.css';

export interface LogEntry {
  timestamp: number;
  type: 'execution' | 'node' | 'output' | 'error';
  message: string;
  metadata?: Record<string, unknown>;
}

interface ExecutionLogStreamProps {
  logs: LogEntry[];
  isRunning: boolean;
}

export function ExecutionLogStream({ logs, isRunning }: ExecutionLogStreamProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const shouldAutoScroll = useRef(true);

  // Auto-scroll to bottom when new logs arrive
  useEffect(() => {
    if (shouldAutoScroll.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs]);

  // Detect if user has scrolled up manually
  const handleScroll = () => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 50;
    shouldAutoScroll.current = isAtBottom;
  };

  const formatTimestamp = (timestamp: number) => {
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-US', {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      fractionalSecondDigits: 1,
    });
  };

  const getLogPrefix = (entry: LogEntry) => {
    switch (entry.type) {
      case 'execution':
        return '>';
      case 'node':
        return '├─';
      case 'output':
        return '└─';
      case 'error':
        return '✗';
      default:
        return '•';
    }
  };

  const getLogClass = (entry: LogEntry) => {
    const baseClass = 'log-entry';
    return `${baseClass} log-entry--${entry.type}`;
  };

  return (
    <div className="execution-log-stream">
      <div className="log-stream-header">
        <span className="log-stream-title">EXECUTION STREAM</span>
        {isRunning && <span className="log-stream-indicator">●</span>}
      </div>
      <div
        ref={containerRef}
        className="log-stream-container"
        onScroll={handleScroll}
      >
        {logs.length === 0 ? (
          <div className="log-empty">
            <span className="cursor-blink">_</span>
            <span className="log-hint">Waiting for execution...</span>
          </div>
        ) : (
          logs.map((entry, index) => (
            <div key={index} className={getLogClass(entry)}>
              <span className="log-timestamp">{formatTimestamp(entry.timestamp)}</span>
              <span className="log-prefix">{getLogPrefix(entry)}</span>
              <span className="log-message">{entry.message}</span>
            </div>
          ))
        )}
        {isRunning && logs.length > 0 && (
          <div className="log-entry">
            <span className="cursor-blink">_</span>
          </div>
        )}
      </div>
    </div>
  );
}
