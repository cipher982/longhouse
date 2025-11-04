import { type FormEvent } from "react";
import clsx from "clsx";
import { Workflow } from "../../services/api";

interface ChatComposerProps {
  draft: string;
  onDraftChange: (draft: string) => void;
  onSend: (evt: FormEvent) => void;
  effectiveThreadId: number | null;
  isSending: boolean;
  showWorkflowPanel: boolean;
  onToggleWorkflowPanel: () => void;
  workflowsQuery: {
    data: Workflow[] | undefined;
    isLoading: boolean;
  };
  selectedWorkflow: number | null;
  onSelectWorkflow: (workflowId: number | null) => void;
  onExecuteWorkflow: () => void;
  isExecutingWorkflow: boolean;
  messagesCount: number;
  onExportChat: () => void;
}

export function ChatComposer({
  draft,
  onDraftChange,
  onSend,
  effectiveThreadId,
  isSending,
  showWorkflowPanel,
  onToggleWorkflowPanel,
  workflowsQuery,
  selectedWorkflow,
  onSelectWorkflow,
  onExecuteWorkflow,
  isExecutingWorkflow,
  messagesCount,
  onExportChat,
}: ChatComposerProps) {
  return (
    <>
      {/* Chat Tools Bar */}
      <div className="chat-tools">
        <button
          type="button"
          className="tool-btn"
          onClick={onToggleWorkflowPanel}
          title="Execute Workflow"
        >
          🔧 Workflows
        </button>
        <button
          type="button"
          className="tool-btn"
          onClick={onExportChat}
          disabled={messagesCount === 0}
          title="Export Chat History"
        >
          📄 Export
        </button>
      </div>

      <form className="chat-input-area" onSubmit={onSend}>
        <input
          type="text"
          value={draft}
          onChange={(evt) => onDraftChange(evt.target.value)}
          placeholder={effectiveThreadId ? "Type your message..." : "Select a thread to start chatting"}
          className="chat-input"
          data-testid="chat-input"
          disabled={!effectiveThreadId}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSend(e as FormEvent);
            }
          }}
        />
        <button
          type="submit"
          className={clsx("send-button", { disabled: !effectiveThreadId })}
          disabled={isSending || !draft.trim() || !effectiveThreadId}
          data-testid="send-message-btn"
        >
          {isSending ? "Sending…" : "Send"}
        </button>
      </form>

      {/* Workflow Execution Panel */}
      {showWorkflowPanel && (
        <div className="workflow-panel">
          <div className="workflow-panel-header">
            <h4>Execute Workflow</h4>
            <button
              type="button"
              className="close-panel-btn"
              onClick={onToggleWorkflowPanel}
            >
              ✕
            </button>
          </div>
          <div className="workflow-panel-content">
            {workflowsQuery.isLoading ? (
              <div>Loading workflows...</div>
            ) : workflowsQuery.data?.length ? (
              <>
                <div className="workflow-selector">
                  <label htmlFor="workflow-select">Select Workflow:</label>
                  <select
                    id="workflow-select"
                    value={selectedWorkflow || ""}
                    onChange={(e) => onSelectWorkflow(Number(e.target.value) || null)}
                  >
                    <option value="">Choose a workflow...</option>
                    {workflowsQuery.data.map((workflow) => (
                      <option key={workflow.id} value={workflow.id}>
                        {workflow.name}
                      </option>
                    ))}
                  </select>
                </div>
                <button
                  type="button"
                  className="execute-workflow-btn"
                  onClick={onExecuteWorkflow}
                  disabled={!selectedWorkflow || isExecutingWorkflow}
                >
                  {isExecutingWorkflow ? "Executing..." : "▶️ Execute"}
                </button>
              </>
            ) : (
              <div className="no-workflows">
                <p>No workflows available</p>
                <small>Create workflows in the Canvas Editor to execute them here</small>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
