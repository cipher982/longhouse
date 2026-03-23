import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import clsx from "clsx";
import { useShelf } from "../lib/useShelfState";
import { Button, EmptyState, Spinner } from "../components/ui";
import { SettingsIcon, SidebarIcon } from "../components/icons";
import AutomationSettingsDrawer from "../components/automation-settings/AutomationSettingsDrawer";
import { usePageMeta } from "../hooks/usePageMeta";
import { ChatThreadList } from "../components/chat/ChatThreadList";
import { ChatMessageList } from "../components/chat/ChatMessageList";
import { ChatComposer } from "../components/chat/ChatComposer";
import { useChatData } from "../hooks/chat/useChatData";
import { useChatActions } from "../hooks/chat/useChatActions";
import { useThreadStreaming } from "../hooks/chat/useThreadStreaming";
import { createThread, type Thread, type ThreadMessage } from "../services/api";
import { parseUTC } from "../lib/dateUtils";

function useRequiredNumber(param?: string): number | null {
  if (!param) return null;
  const parsed = Number(param);
  return Number.isFinite(parsed) ? parsed : null;
}

function buildThreadPath(automationId: number, threadId?: number | null): string {
  return threadId == null
    ? `/automations/${automationId}/thread`
    : `/automations/${automationId}/thread/${threadId}`;
}

function isReloadNavigation() {
  if (typeof performance === "undefined") {
    return false;
  }

  const legacyNavigation = performance as Performance & {
    navigation?: { type?: number; TYPE_RELOAD?: number };
  };

  if (
    typeof legacyNavigation.navigation?.type === "number" &&
    legacyNavigation.navigation.type === legacyNavigation.navigation.TYPE_RELOAD
  ) {
    return true;
  }

  if (typeof performance.getEntriesByType !== "function") {
    return false;
  }

  const entries = performance.getEntriesByType("navigation");
  const latest = entries[entries.length - 1] as PerformanceNavigationTiming | undefined;
  return latest?.type === "reload";
}

type InitialThreadBootstrapState = {
  automationId: number | null;
  status: "idle" | "creating" | "failed";
};

export default function ChatPage() {
  const params = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isShelfOpen, closeShelf, toggleShelf } = useShelf();
  const initialThreadAttemptRef = useRef<number | null>(null);

  const automationId = useRequiredNumber(params.automationId);
  const threadIdParam = useRequiredNumber(params.threadId ?? undefined);
  const [editingThreadId, setEditingThreadId] = useState<number | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [isSettingsDrawerOpen, setIsSettingsDrawerOpen] = useState(false);
  const [initialThreadBootstrap, setInitialThreadBootstrap] = useState<InitialThreadBootstrapState>({
    automationId: null,
    status: "idle",
  });

  // Advanced features state
  const [draft, setDraft] = useState("");

  // Use chat data hook - strict URL state (no fallback)
  const { automation, chatThreads, automationThreads, messages, isLoading, hasError, chatThreadsQuery } = useChatData({
    automationId,
    effectiveThreadId: threadIdParam,
  });

  const effectiveThreadId = threadIdParam;
  usePageMeta({
    title: automation ? `${automation.name} - Longhouse` : "Longhouse",
  });
  const shouldRedirectReload = useMemo(() => isReloadNavigation(), []);

  const needsThreadRoute = automationId != null && effectiveThreadId == null;
  const initialThreadState =
    effectiveThreadId == null && initialThreadBootstrap.automationId === automationId
      ? initialThreadBootstrap.status
      : "idle";
  const shouldAutoCreateInitialThread =
    needsThreadRoute &&
    !chatThreadsQuery.isLoading &&
    chatThreads.length === 0 &&
    initialThreadState === "idle";

  useEffect(() => {
    const initializeThread = async () => {
      if (
        automationId == null ||
        !shouldAutoCreateInitialThread ||
        initialThreadAttemptRef.current === automationId
      ) {
        return;
      }

      initialThreadAttemptRef.current = automationId;
      setInitialThreadBootstrap({ automationId, status: "creating" });

      try {
        const thread = await createThread(automationId, "Thread 1");
        await queryClient.invalidateQueries({ queryKey: ["threads", automationId, "chat"] });
        navigate(buildThreadPath(automationId, thread.id), { replace: true });
      } catch (error) {
        console.error('[ChatPage] Failed to auto-create default thread:', error);
        setInitialThreadBootstrap({ automationId, status: "failed" });
        initialThreadAttemptRef.current = null;
        toast.error('Failed to create default chat thread. Please try creating one manually.');
      }
    };

    if (shouldAutoCreateInitialThread) {
      void initializeThread();
    }
  }, [automationId, navigate, queryClient, shouldAutoCreateInitialThread]);

  // Use chat actions hook
  const { sendMutation, renameThreadMutation } = useChatActions({
    automationId,
    effectiveThreadId,
  });

  // Use streaming hook - no subscriptions needed, user:{user_id} is auto-subscribed
  const { streamingMessages, streamingMessageId, pendingTokenBuffer, allStreamingThreadIds } = useThreadStreaming({
    automationId,
    effectiveThreadId,
  });

  // Event handlers
  const handleSelectThread = (thread: Thread) => {
    if (automationId == null) {
      return;
    }
    navigate(buildThreadPath(automationId, thread.id), { replace: true });
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

    if (automationId == null) {
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
        const timestamp = msg.created_at ? parseUTC(msg.created_at).toLocaleString() : "";
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

  if (automationId == null) {
    return (
      <div className="chat-view-container" data-testid="chat-page">
        <EmptyState
          variant="error"
          title="Missing automation context"
          description="Open the timeline to pick a session or return to the main app."
          action={<Button variant="primary" onClick={() => navigate("/timeline")}>Go to Timeline</Button>}
        />
      </div>
    );
  }

  const handleCreateThread = async () => {
    if (automationId == null) return;
    // Auto-generate thread name based on the count of existing threads
    const threadCount = chatThreads.length + 1;
    const title = `Thread ${threadCount}`;
    try {
      const thread = await createThread(automationId, title);
      await queryClient.invalidateQueries({ queryKey: ["threads", automationId, "chat"] });
      navigate(buildThreadPath(automationId, thread.id), { replace: true });
    } catch {
      toast.error("Failed to create thread", { duration: 6000 });
    }
  };

  if (shouldRedirectReload) {
    return <Navigate to="/timeline" replace />;
  }

  if (needsThreadRoute && !chatThreadsQuery.isLoading && chatThreads.length > 0) {
    return <Navigate to={buildThreadPath(automationId, chatThreads[0].id)} replace />;
  }

  if (needsThreadRoute && chatThreads.length === 0 && initialThreadState === "failed") {
    return (
      <div className="chat-view-container" data-testid="chat-page">
        <EmptyState
          variant="error"
          title="Could not open chat"
          description="Longhouse could not create the initial chat thread automatically."
          action={<Button variant="primary" onClick={handleCreateThread}>Create Thread</Button>}
        />
      </div>
    );
  }

  if (needsThreadRoute && chatThreads.length === 0) {
    return (
      <div className="chat-view-container" data-testid="chat-page">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Preparing chat..."
          description="Setting up the first thread for this automation."
        />
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="chat-view-container" data-testid="chat-page">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading chat..."
          description="Fetching your conversation."
        />
      </div>
    );
  }

  if (hasError) {
    return (
      <div className="chat-view-container" data-testid="chat-page">
        <EmptyState
          variant="error"
          title="Unable to load chat"
          description="Something went wrong loading the conversation."
          action={<Button variant="primary" onClick={() => navigate("/timeline")}>Back to Timeline</Button>}
        />
      </div>
    );
  }

  return (
    <>
      <div id="chat-view-container" className="chat-view-container" data-testid="chat-page">
        <header className="chat-header">
          <button
            type="button"
            className="back-button"
            onClick={() => navigate("/timeline")}
            aria-label="Back to timeline"
          >
            ←
          </button>
          <div className="automation-info">
            <div className="automation-name">{automation?.name ?? "Automation"}</div>
            <div>
              <span className="thread-title-label">Thread: </span>
              <span className="thread-title-text">
                {effectiveThreadId != null ? `#${effectiveThreadId}` : "None"}
              </span>
            </div>
          </div>
          {automationId != null && (
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
                title="Automation configuration settings"
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
            threadId={effectiveThreadId}
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
            messagesCount={messages.length}
            onExportChat={handleExportChat}
          />
        </div>
      </div>
      {automationId != null && (
        <AutomationSettingsDrawer
          automationId={automationId}
          isOpen={isSettingsDrawerOpen}
          onClose={() => setIsSettingsDrawerOpen(false)}
        />
      )}
    </>
  );
}
