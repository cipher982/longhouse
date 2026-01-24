import { useState } from "react";
import clsx from "clsx";
import { Thread } from "../../services/api";
import { formatTimestamp, truncateText } from "./chatUtils";

interface ChatThreadListProps {
  chatThreads: Thread[];
  automationThreads: Thread[];
  effectiveThreadId: number | null;
  editingThreadId: number | null;
  editingTitle: string;
  onSelectThread: (thread: Thread) => void;
  onEditThreadTitle: (thread: Thread, e: React.MouseEvent) => void;
  onSaveThreadTitle: (threadId: number) => void;
  onCancelEdit: () => void;
  onTitleChange: (title: string) => void;
  isRenamingPending: boolean;
  onCreateThread: () => void;
  isShelfOpen?: boolean;
  streamingThreadIds: number[];
}

export function ChatThreadList({
  chatThreads,
  automationThreads,
  effectiveThreadId,
  editingThreadId,
  editingTitle,
  onSelectThread,
  onEditThreadTitle,
  onSaveThreadTitle,
  onCancelEdit,
  onTitleChange,
  isRenamingPending,
  onCreateThread,
  isShelfOpen,
  streamingThreadIds,
}: ChatThreadListProps) {
  const [isAutomationCollapsed, setIsAutomationCollapsed] = useState(true);

  return (
    <aside id="thread-sidebar" className={clsx("thread-sidebar", { active: isShelfOpen })}>
      <div className="sidebar-header">
        <h3>Threads</h3>
        <button
          type="button"
          className="new-thread-btn"
          data-testid="new-thread-btn"
          onClick={onCreateThread}
        >
          New Thread
        </button>
      </div>
      <div className="thread-list">
        {chatThreads.map((thread) => {
          const threadMessages = (thread.messages || []).filter(m => m.role !== "system");
          const lastMessage = threadMessages[threadMessages.length - 1];
          const messagePreview = lastMessage
            ? truncateText(lastMessage.content, 50)
            : "No messages";
          const isWriting = streamingThreadIds.includes(thread.id);

          return (
            <div
              key={thread.id}
              className={clsx("thread-item", {
                selected: thread.id === effectiveThreadId,
                writing: isWriting
              })}
              data-testid={`thread-row-${thread.id}`}
              data-id={thread.id}
              data-thread-id={thread.id}
              role="button"
              tabIndex={0}
              onClick={() => onSelectThread(thread)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelectThread(thread);
                }
              }}
            >
              {editingThreadId === thread.id ? (
                <div className="thread-edit-form" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="text"
                    value={editingTitle}
                    onChange={(e) => onTitleChange(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        onSaveThreadTitle(thread.id);
                      } else if (e.key === "Escape") {
                        onCancelEdit();
                      }
                    }}
                    autoFocus
                    className="thread-title-input"
                    disabled={isRenamingPending}
                  />
                </div>
              ) : (
                <>
                  <div className="thread-item-title">
                    {isWriting && <span className="writing-indicator" title="AI is writing...">✍️ </span>}
                    {thread.title}
                  </div>
                  <div className="thread-item-time">
                    {formatTimestamp(thread.updated_at || thread.created_at)}
                  </div>
                  <button
                    type="button"
                    className="thread-edit-button"
                    data-testid={`edit-thread-${thread.id}`}
                    onClick={(e) => onEditThreadTitle(thread, e)}
                    aria-label="Edit thread title"
                    title="Edit thread title"
                    disabled={isRenamingPending}
                  >
                    ✎
                  </button>
                  <div className="thread-item-preview">{messagePreview}</div>
                </>
              )}
            </div>
          );
        })}
        {chatThreads.length === 0 && (
          <div className="thread-list-empty">No threads found</div>
        )}
      </div>

      {/* Automation History Section */}
      {automationThreads.length > 0 && (
        <div className="automation-history" data-testid="automation-history">
          <div
            className="automation-history-header"
            data-testid="automation-history-header"
            onClick={() => setIsAutomationCollapsed(!isAutomationCollapsed)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                setIsAutomationCollapsed(!isAutomationCollapsed);
              }
            }}
          >
            <h4 className="automation-history-title">
              <span
                className={clsx("automation-collapse-icon", { collapsed: isAutomationCollapsed })}
                data-testid="automation-collapse-icon"
              >
                ▼
              </span>
              Automation Runs
            </h4>
            <span className="automation-history-count" data-testid="automation-count">
              {automationThreads.length}
            </span>
          </div>
          <div
            className={clsx("automation-runs-list", { collapsed: isAutomationCollapsed })}
            data-testid="automation-runs-list"
          >
            {automationThreads.map((thread) => (
              <div
                key={thread.id}
                className={clsx("automation-run-item", {
                  selected: thread.id === effectiveThreadId,
                })}
                data-testid={`automation-run-${thread.id}`}
                data-id={thread.id}
                data-thread-id={thread.id}
                data-thread-type={thread.thread_type}
                role="button"
                tabIndex={0}
                onClick={() => onSelectThread(thread)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelectThread(thread);
                  }
                }}
              >
                <div className="automation-run-title">{thread.title}</div>
                <div className="automation-run-time">
                  {formatTimestamp(thread.created_at)}
                </div>
                <div className="automation-run-type">
                  <span
                    className={`run-badge run-badge-${thread.thread_type}`}
                    data-testid={`run-badge-${thread.thread_type}`}
                  >
                    {thread.thread_type === "scheduled" ? "🔄 Scheduled" : "▶️ Manual"}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}
