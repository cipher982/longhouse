import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import clsx from "clsx";
import { useShelf } from "../lib/useShelfState";
import { SettingsIcon, SidebarIcon } from "../components/icons";
import AgentSettingsDrawer from "../components/agent-settings/AgentSettingsDrawer";
import { ChatThreadList } from "../components/chat/ChatThreadList";
import { ChatMessageList } from "../components/chat/ChatMessageList";
import { ChatComposer } from "../components/chat/ChatComposer";
import { useChatData } from "../hooks/chat/useChatData";
import { useChatActions } from "../hooks/chat/useChatActions";
import { useThreadStreaming } from "../hooks/chat/useThreadStreaming";
import { createThread, type Thread, type ThreadMessage } from "../services/api";

function useRequiredNumber(param?: string): number | null {
  if (!param) return null;
  const parsed = Number(param);
  return Number.isFinite(parsed) ? parsed : null;
}

export default function ChatPage() {
  const params = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isShelfOpen, closeShelf, toggleShelf } = useShelf();
  const creatingThreadRef = useRef(false);

  const agentId = useRequiredNumber(params.agentId);
  const threadIdParam = useRequiredNumber(params.threadId ?? undefined);
  const [selectedThreadId, setSelectedThreadId] = useState<number | null>(threadIdParam);
  const [editingThreadId, setEditingThreadId] = useState<number | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [isSettingsDrawerOpen, setIsSettingsDrawerOpen] = useState(false);

  // Advanced features state
  const [showWorkflowPanel, setShowWorkflowPanel] = useState(false);
  const [selectedWorkflow, setSelectedWorkflow] = useState<number | null>(null);
  const [draft, setDraft] = useState("");

  // Sync selectedThreadId from URL parameter - one-way only (URL → state)
  // This ensures state stays consistent with URL, but doesn't override when
  // we're intentionally changing threads via state updates
  useEffect(() => {
    setSelectedThreadId(threadIdParam ?? null);
  }, [threadIdParam]);

  // Use chat data hook - strict URL state (no fallback)
  const { agent, chatThreads, automationThreads, messages, isLoading, hasError, workflowsQuery, chatThreadsQuery } = useChatData({
    agentId,
    effectiveThreadId: selectedThreadId,
  });

  // Strict URL model: effectiveThreadId is just selectedThreadId
  // If no thread is selected, we handle it explicitly below
  const effectiveThreadId = selectedThreadId;

  // Handle navigation reload
  useEffect(() => {
    if (typeof performance === "undefined") {
      return;
    }

    const legacyNav = (performance as Performance & { navigation?: PerformanceNavigation }).navigation;
    if (legacyNav && legacyNav.type === legacyNav.TYPE_RELOAD) {
      navigate("/dashboard", { replace: true });
      return;
    }

    if (typeof performance.getEntriesByType === "function") {
      const entries = performance.getEntriesByType("navigation");
      const latest = entries[entries.length - 1] as PerformanceNavigationTiming | undefined;
      if (latest?.type === "reload") {
        navigate("/dashboard", { replace: true });
      }
    }
  }, [navigate]);

  // Handle URL navigation
  useEffect(() => {
    if (agentId != null && effectiveThreadId != null) {
      navigate(`/agent/${agentId}/thread/${effectiveThreadId}`, { replace: true });
    } else if (agentId != null) {
      navigate(`/agent/${agentId}/thread/`, { replace: true });
    }
  }, [agentId, effectiveThreadId, navigate]);

  // Auto-select most recent thread if threads exist but none selected
  // This handles the case when navigating to /agent/{id}/thread/ with existing threads
  useEffect(() => {
    if (
      agentId != null &&
      selectedThreadId == null &&
      !chatThreadsQuery.isLoading &&
      chatThreads.length > 0
    ) {
      // Auto-select the first (most recent) thread
      const mostRecentThread = chatThreads[0];
      setSelectedThreadId(mostRecentThread.id);
      navigate(`/agent/${agentId}/thread/${mostRecentThread.id}`, { replace: true });
    }
  }, [agentId, selectedThreadId, chatThreads, chatThreadsQuery.isLoading, navigate]);

  // Auto-create a default thread on component mount if none exists
  useEffect(() => {
    const initializeThread = async () => {
      if (agentId == null || selectedThreadId != null || chatThreads.length > 0 || creatingThreadRef.current) {
        return;
      }

      creatingThreadRef.current = true;

      try {
        // Auto-create a default thread for the agent
        const thread = await createThread(agentId, "Thread 1");
        await queryClient.invalidateQueries({ queryKey: ["threads", agentId, "chat"] });
        setSelectedThreadId(thread.id);
        navigate(`/agent/${agentId}/thread/${thread.id}`, { replace: true });
      } catch (error) {
        console.error('[ChatPage] Failed to auto-create default thread:', error);
        toast.error('Failed to create default chat thread. Please try creating one manually.');
      } finally {
        creatingThreadRef.current = false;
      }
    };

    // Only create thread if:
    // 1. We have an agentId
    // 2. No thread is selected
    // 3. The query has finished loading (not in loading state)
    // 4. There are no chat threads
    if (agentId != null && selectedThreadId == null && !chatThreadsQuery.isLoading && chatThreads.length === 0) {
      initializeThread();
    }
  }, [agentId, selectedThreadId, chatThreads.length, chatThreadsQuery.isLoading, queryClient, navigate]);

  // Use chat actions hook
  const { sendMutation, executeWorkflowMutation, renameThreadMutation } = useChatActions({
    agentId,
    effectiveThreadId,
  });

  // Use streaming hook - no subscriptions needed, user:{user_id} is auto-subscribed
  const { streamingMessages, streamingMessageId, pendingTokenBuffer, allStreamingThreadIds } = useThreadStreaming({
    agentId,
    effectiveThreadId,
  });

  // Event handlers
  const handleSelectThread = (thread: Thread) => {
    setSelectedThreadId(thread.id);
    navigate(`/agent/${agentId}/thread/${thread.id}`, { replace: true });
  };

  const handleEditThreadTitle = (thread: Thread, e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingThreadId(thread.id);
    setEditingTitle(thread.title);
  };

  const handleSaveThreadTitle = async (threadId: number) => {
    const trimmedTitle = editingTitle.trim();
    if (!trimmedTitle) {
      handleCancelEdit();
      return;
    }

    const existingThread = chatThreads.find((thread) => thread.id === threadId);
    if (existingThread && existingThread.title === trimmedTitle) {
      handleCancelEdit();
      return;
    }

    if (agentId == null) {
      return;
    }

    try {
      await renameThreadMutation.mutateAsync({ threadId, title: trimmedTitle });
      setEditingThreadId(null);
      setEditingTitle("");
    } catch {
      // Error handling is done in the mutation's onError callback
    }
  };

  const handleCancelEdit = () => {
    setEditingThreadId(null);
    setEditingTitle("");
  };

  // Event handlers
  const handleSend = async (evt: FormEvent) => {
    evt.preventDefault();
    if (effectiveThreadId == null) {
      toast.error("Please select a thread first");
      return;
    }
    const trimmed = draft.trim();
    if (!trimmed) {
      return;
    }
    setDraft("");
    try {
      await sendMutation.mutateAsync({ threadId: effectiveThreadId, content: trimmed });
    } catch {
      // Error handling is done in the mutation's onError callback
    }
  };

  // Message action handlers
  const handleCopyMessage = (message: ThreadMessage) => {
    navigator.clipboard.writeText(message.content).then(() => {
      toast.success("Message copied to clipboard");
    }).catch(() => {
      toast.error("Failed to copy message");
    });
  };

  const handleExportChat = () => {
    if (messages.length === 0) {
      toast.error("No messages to export");
      return;
    }

    const chatHistory = messages
      .filter(msg => msg.role !== "system")
      .map(msg => {
        const timestamp = new Date(msg.created_at || "").toLocaleString();
        return `[${timestamp}] ${msg.role.toUpperCase()}: ${msg.content}`;
      })
      .join("\n\n");

    const blob = new Blob([chatHistory], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `chat-history-${effectiveThreadId || 'unknown'}.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);

    toast.success("Chat history exported");
  };

  // Workflow execution handler
  const handleExecuteWorkflow = () => {
    if (!selectedWorkflow) {
      toast.error("Please select a workflow");
      return;
    }
    executeWorkflowMutation.mutate({ workflowId: selectedWorkflow });
    setShowWorkflowPanel(false);
  };

  // Update document title with agent name for better context
  useEffect(() => {
    if (agent) {
      document.title = `${agent.name} - Swarmlet`;
    }
    return () => {
      document.title = "Swarmlet AI Agent Platform";
    };
  }, [agent]);

  if (agentId == null) {
    return <div>Missing agent context.</div>;
  }

  const handleCreateThread = async () => {
    if (agentId == null) return;
    // Auto-generate thread name based on the count of existing threads
    const threadCount = chatThreads.length + 1;
    const title = `Thread ${threadCount}`;
    try {
      const thread = await createThread(agentId, title);
      queryClient.invalidateQueries({ queryKey: ["threads", agentId, "chat"] });
      // Navigate to the new thread - strict URL state
      navigate(`/agent/${agentId}/thread/${thread.id}`, { replace: true });
    } catch {
      toast.error("Failed to create thread", { duration: 6000 });
    }
  };

  if (isLoading) {
    return <div>Loading chat…</div>;
  }

  if (hasError) {
    return <div>Unable to load chat view.</div>;
  }

  return (
    <>
      <div id="chat-view-container" className="chat-view-container" data-testid="chat-page">
        <header className="chat-header">
          <button
            type="button"
            className="back-button"
            onClick={() => navigate("/dashboard")}
            aria-label="Back to dashboard"
          >
            ←
          </button>
          <div className="agent-info">
            <div className="agent-name">{agent?.name ?? "Agent"}</div>
            <div>
              <span className="thread-title-label">Thread: </span>
              <span className="thread-title-text">
                {effectiveThreadId != null ? `#${effectiveThreadId}` : "None"}
              </span>
            </div>
          </div>
          {agentId != null && (
            <div className="chat-actions">
              <button
                type="button"
                className={clsx("chat-settings-btn", "chat-thread-toggle", {
                  "chat-thread-toggle--active": isShelfOpen,
                })}
                onClick={toggleShelf}
                title={isShelfOpen ? "Hide threads" : "Show threads"}
                aria-expanded={isShelfOpen}
                aria-controls="thread-sidebar"
              >
                <SidebarIcon />
                <span>Threads</span>
              </button>
              <button
                type="button"
                className="chat-settings-btn"
                onClick={() => setIsSettingsDrawerOpen(true)}
                title="Agent configuration settings"
              >
                <SettingsIcon />
                <span>Config</span>
              </button>
            </div>
          )}
        </header>

        <div className="chat-body">
          <ChatThreadList
            chatThreads={chatThreads}
            automationThreads={automationThreads}
            effectiveThreadId={effectiveThreadId}
            editingThreadId={editingThreadId}
            editingTitle={editingTitle}
            onSelectThread={handleSelectThread}
            onEditThreadTitle={handleEditThreadTitle}
            onSaveThreadTitle={handleSaveThreadTitle}
            onCancelEdit={handleCancelEdit}
            onTitleChange={setEditingTitle}
            isRenamingPending={renameThreadMutation.isPending}
            onCreateThread={handleCreateThread}
            isShelfOpen={isShelfOpen}
            streamingThreadIds={allStreamingThreadIds}
          />

          <ChatMessageList
            messages={messages}
            streamingMessages={streamingMessages}
            streamingMessageId={streamingMessageId}
            pendingTokenBuffer={pendingTokenBuffer}
            onCopyMessage={handleCopyMessage}
          />
        </div>

        {/* Scrim overlay when thread sidebar is open on mobile */}
        <div
          className={clsx("thread-scrim", { "thread-scrim--visible": isShelfOpen })}
          onClick={closeShelf}
        />

        {/* Chat Input Area */}
        <div className="chat-input-wrapper">
          <ChatComposer
            draft={draft}
            onDraftChange={setDraft}
            onSend={handleSend}
            effectiveThreadId={effectiveThreadId}
            isSending={sendMutation.isPending}
            showWorkflowPanel={showWorkflowPanel}
            onToggleWorkflowPanel={() => setShowWorkflowPanel(!showWorkflowPanel)}
            workflowsQuery={workflowsQuery}
            selectedWorkflow={selectedWorkflow}
            onSelectWorkflow={setSelectedWorkflow}
            onExecuteWorkflow={handleExecuteWorkflow}
            isExecutingWorkflow={executeWorkflowMutation.isPending}
            messagesCount={messages.length}
            onExportChat={handleExportChat}
          />
        </div>
      </div>
      {agentId != null && (
        <AgentSettingsDrawer
          agentId={agentId}
          isOpen={isSettingsDrawerOpen}
          onClose={() => setIsSettingsDrawerOpen(false)}
        />
      )}
    </>
  );
}
