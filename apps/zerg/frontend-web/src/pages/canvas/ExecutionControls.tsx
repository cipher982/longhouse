import type { Node as FlowNode } from "@xyflow/react";
import type { Workflow, ExecutionStatus } from "../../services/api";

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
              className={`run-button ${isPending ? 'loading' : ''}`}
              onClick={onRun}
              disabled={isDisabled}
              title={tooltip}
            >
              {isPending ? '⏳' : '▶️'} Run
            </button>
          );
        })()}

        {currentExecution?.phase === 'running' && (
          <button
            className="cancel-button"
            onClick={onCancel}
            disabled={isPending}
            title="Cancel Execution"
          >
            ⏹️ Cancel
          </button>
        )}

        {currentExecution && (
          <button
            className="logs-button"
            onClick={onToggleLogs}
            title="Toggle Execution Logs"
            aria-expanded={showLogs}
            aria-controls="execution-logs-drawer"
          >
            📋 Logs {showLogs ? '▼' : '▶️'}
          </button>
        )}

        <div className="canvas-mode-toggles" role="group" aria-label="Canvas display toggles">
          <button
            type="button"
            className="canvas-toggle-btn"
            onClick={onToggleSnapToGrid}
            aria-pressed={snapToGridEnabled}
            aria-label={snapToGridEnabled ? 'Disable snap to grid (Shift+S)' : 'Enable snap to grid (Shift+S)'}
            title={`Snap to grid ${snapToGridEnabled ? 'enabled' : 'disabled'} (Shift+S)`}
          >
            ⬛
          </button>
          <button
            type="button"
            className="canvas-toggle-btn"
            onClick={onToggleGuides}
            aria-pressed={guidesVisible}
            aria-label={guidesVisible ? 'Hide guides (Shift+G)' : 'Show guides (Shift+G)'}
            title={`Guides ${guidesVisible ? 'visible' : 'hidden'} (Shift+G)`}
          >
            #️⃣
          </button>
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
          <span className="execution-phase">
            {currentExecution.phase === 'waiting' && '⏳ Waiting'}
            {currentExecution.phase === 'running' && '🔄 Running'}
            {currentExecution.phase === 'finished' && '✅ Finished'}
            {currentExecution.phase === 'cancelled' && '❌ Cancelled'}
          </span>
          <span className="execution-id">ID: {currentExecution.execution_id}</span>
          <span className="execution-toggle-hint" style={{ fontSize: '0.8em', opacity: 0.7, marginLeft: '8px' }}>
            {showLogs ? '▼' : '▶'}
          </span>
        </div>
      )}
    </div>
  );
}
