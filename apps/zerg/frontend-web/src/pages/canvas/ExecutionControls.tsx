import type { Node as FlowNode } from "@xyflow/react";
import type { Workflow, ExecutionStatus } from "../../services/api";
import {
  PlayIcon,
  SquareIcon,
  ClipboardListIcon,
  GridIcon,
  HashIcon,
  ChevronDownIcon,
  ChevronRightIcon
} from "../../components/icons";
import { IconButton } from "../../components/ui/IconButton";

interface ExecutionControlsProps {
  workflow: Workflow | undefined;
  nodes: FlowNode[];
  currentExecution: ExecutionStatus | null;
  showLogs: boolean;
  snapToGridEnabled: boolean;
  guidesVisible: boolean;
  isPending: boolean;
  onRun: () => void;
  onCancel: () => void;
  onToggleLogs: () => void;
  onToggleSnapToGrid: () => void;
  onToggleGuides: () => void;
}

export function ExecutionControls({
  workflow,
  nodes,
  currentExecution,
  showLogs,
  snapToGridEnabled,
  guidesVisible,
  isPending,
  onRun,
  onCancel,
  onToggleLogs,
  onToggleSnapToGrid,
  onToggleGuides,
}: ExecutionControlsProps) {
  return (
    <div className="execution-controls">
      <div className="execution-buttons">
        {(() => {
          const hasNodes = nodes.length > 0;
          const isRunning = currentExecution?.phase === 'running';
          const noWorkflow = !workflow?.id;
          const isDisabled = isPending || noWorkflow || isRunning || !hasNodes;

          // Determine the appropriate tooltip
          let tooltip = "Run Workflow";
          if (isPending) tooltip = "Starting workflow...";
          else if (isRunning) tooltip = "Workflow is already running";
          else if (noWorkflow) tooltip = "No workflow loaded";
          else if (!hasNodes) tooltip = "Add nodes to the canvas before running";

          return (
            <button
              className={`ui-button ui-button--success run-button ${isPending ? 'loading' : ''}`}
              onClick={onRun}
              disabled={isDisabled}
              title={tooltip}
            >
              {isPending ? <span className="animate-spin">⏳</span> : <PlayIcon width={14} height={14} fill="currentColor" />}
              <span>Run</span>
            </button>
          );
        })()}

        {currentExecution?.phase === 'running' && (
          <button
            className="ui-button ui-button--danger cancel-button"
            onClick={onCancel}
            disabled={isPending}
            title="Cancel Execution"
          >
            <SquareIcon width={14} height={14} fill="currentColor" />
            <span>Cancel</span>
          </button>
        )}

        {currentExecution && (
          <button
            className={`ui-button ui-button--secondary logs-button ${showLogs ? 'active' : ''}`}
            onClick={onToggleLogs}
            title="Toggle Execution Logs"
            aria-expanded={showLogs}
            aria-controls="execution-logs-drawer"
          >
            <ClipboardListIcon width={14} height={14} />
            <span>Logs</span>
            {showLogs ? <ChevronDownIcon width={14} height={14} /> : <ChevronRightIcon width={14} height={14} />}
          </button>
        )}

        <div className="canvas-mode-toggles" role="group" aria-label="Canvas display toggles">
          <IconButton
            type="button"
            className={`canvas-toggle-btn ${snapToGridEnabled ? 'active' : ''}`}
            onClick={onToggleSnapToGrid}
            aria-pressed={snapToGridEnabled}
            aria-label={snapToGridEnabled ? 'Disable snap to grid (Shift+S)' : 'Enable snap to grid (Shift+S)'}
            title={`Snap to grid ${snapToGridEnabled ? 'enabled' : 'disabled'} (Shift+S)`}
          >
            <GridIcon width={16} height={16} />
          </IconButton>
          <IconButton
            type="button"
            className={`canvas-toggle-btn ${guidesVisible ? 'active' : ''}`}
            onClick={onToggleGuides}
            aria-pressed={guidesVisible}
            aria-label={guidesVisible ? 'Hide guides (Shift+G)' : 'Show guides (Shift+G)'}
            title={`Guides ${guidesVisible ? 'visible' : 'hidden'} (Shift+G)`}
          >
            <HashIcon width={16} height={16} />
          </IconButton>
        </div>
      </div>

      {/* Execution Status */}
      {currentExecution && (
        <div
          className={`execution-status execution-status--${currentExecution.phase}`}
          onClick={onToggleLogs}
          style={{ cursor: 'pointer' }}
          title={showLogs ? "Click to hide execution details" : "Click to show execution details"}
        >
          <div className="status-indicator">
            <span className="status-dot"></span>
            <span className="execution-phase">
              {currentExecution.phase.charAt(0).toUpperCase() + currentExecution.phase.slice(1)}
            </span>
          </div>
          <span className="execution-id">{currentExecution.execution_id}</span>
          <span className="execution-toggle-hint">
            {showLogs ? <ChevronDownIcon width={12} height={12} /> : <ChevronRightIcon width={12} height={12} />}
          </span>
        </div>
      )}
    </div>
  );
}
