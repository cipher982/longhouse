import { useRef, useCallback, useEffect, useState } from "react";
import { ExecutionLogStream, type LogEntry } from "../../components/ExecutionLogStream";
import type { ExecutionStatus } from "../../services/api";
import { XIcon } from "../../components/icons";

interface ExecutionLogsPanelProps {
  showLogs: boolean;
  currentExecution: ExecutionStatus | null;
  executionLogs: LogEntry[];
  onClose: () => void;
}

export function ExecutionLogsPanel({
  showLogs,
  currentExecution,
  executionLogs,
  onClose,
}: ExecutionLogsPanelProps) {
  const logsPanelRef = useRef<HTMLDivElement | null>(null);
  const [logsPanelPosition, setLogsPanelPosition] = useState<{ x: number; y: number } | null>(null);
  const [isDraggingLogsPanel, setIsDraggingLogsPanel] = useState(false);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });

  const handleLogsPanelMouseDown = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    // Only initiate drag if clicking on the header, not the close button
    if ((event.target as HTMLElement).closest('.close-logs')) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    if (!logsPanelRef.current) return;

    const rect = logsPanelRef.current.getBoundingClientRect();
    setDragOffset({
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    });
    setIsDraggingLogsPanel(true);
  }, []);

  const handleLogsPanelMouseMove = useCallback((event: MouseEvent) => {
    if (!isDraggingLogsPanel || !logsPanelRef.current || !logsPanelPosition) return;

    const panel = logsPanelRef.current;
    const panelRect = panel.getBoundingClientRect();

    // Calculate new position
    let newX = event.clientX - dragOffset.x;
    let newY = event.clientY - dragOffset.y;

    // Get viewport bounds
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const panelWidth = panelRect.width;

    // Constrain to viewport bounds (leave at least 50px visible)
    const minVisible = 50;
    newX = Math.max(-panelWidth + minVisible, Math.min(newX, viewportWidth - minVisible));
    newY = Math.max(0, Math.min(newY, viewportHeight - minVisible));

    setLogsPanelPosition({ x: newX, y: newY });
  }, [isDraggingLogsPanel, dragOffset, logsPanelPosition]);

  const handleLogsPanelMouseUp = useCallback(() => {
    setIsDraggingLogsPanel(false);
  }, []);

  // Effect for logs panel dragging
  useEffect(() => {
    if (isDraggingLogsPanel) {
      document.addEventListener('mousemove', handleLogsPanelMouseMove);
      document.addEventListener('mouseup', handleLogsPanelMouseUp);
      return () => {
        document.removeEventListener('mousemove', handleLogsPanelMouseMove);
        document.removeEventListener('mouseup', handleLogsPanelMouseUp);
      };
    }
  }, [isDraggingLogsPanel, handleLogsPanelMouseMove, handleLogsPanelMouseUp]);

  // Initialize panel position when logs are opened
  useEffect(() => {
    if (showLogs && logsPanelPosition === null) {
      // Center the panel
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const panelWidth = 400; // Default width
      const _panelHeight = 500; // Default max-height

      const newPosition = {
        x: (viewportWidth - panelWidth) / 2,
        y: Math.max(80, (viewportHeight - _panelHeight) / 2),
      };

      console.log('[CanvasPage] 🪟 Initializing panel position:', newPosition);
      setLogsPanelPosition(newPosition);
    }
  }, [showLogs, logsPanelPosition]);

  if (!showLogs || !currentExecution) {
    return null;
  }

  return (
    <aside
      ref={logsPanelRef}
      id="execution-logs-drawer"
      className={`execution-logs-draggable ${isDraggingLogsPanel ? 'dragging' : ''}`}
      role="complementary"
      aria-label="Execution logs"
      style={{
        left: logsPanelPosition ? `${logsPanelPosition.x}px` : '50%',
        top: logsPanelPosition ? `${logsPanelPosition.y}px` : '20%',
        transform: logsPanelPosition ? 'none' : 'translateX(-50%)',
      }}
    >
      <div
        className="logs-header"
        onMouseDown={handleLogsPanelMouseDown}
        style={{ cursor: isDraggingLogsPanel ? 'grabbing' : 'grab' }}
      >
        <h4>Execution Logs</h4>
        <button
          className="close-logs"
          onClick={onClose}
          title="Close Logs"
        >
          <XIcon width={14} height={14} />
        </button>
      </div>
      <div className="logs-content">
        <ExecutionLogStream
          logs={executionLogs}
          isRunning={currentExecution.phase === 'running'}
        />
      </div>
    </aside>
  );
}
