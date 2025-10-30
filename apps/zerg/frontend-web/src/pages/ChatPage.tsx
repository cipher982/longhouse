import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import clsx from "clsx";
import {
  Agent,
  createThread,
  fetchAgent,
  fetchThreadMessages,
  fetchThreads,
  postThreadMessage,
  runThread,
  Thread,
  ThreadMessage,
  updateThread,
  fetchWorkflows,
  startWorkflowExecution,
  type Workflow,
} from "../services/api";
import { useShelf } from "../lib/useShelfState";
import { useWebSocket } from "../lib/useWebSocket";
import { SettingsIcon } from "../components/icons";
import AgentSettingsDrawer from "../components/agent-settings/AgentSettingsDrawer";

function useRequiredNumber(param?: string): number | null {
  if (!param) return null;
  const parsed = Number(param);
  return Number.isFinite(parsed) ? parsed : null;
}

// Helper functions
function formatTimestamp(sentAt?: string | null): string {
  if (!sentAt) return "";
  try {
    const date = new Date(sentAt);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return "";
  }
}

function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + "...";
}


// ToolMessage component for rendering collapsible tool call details
interface ToolMessageProps {
  message: ThreadMessage;
}

function ToolMessage({ message }: ToolMessageProps) {
  const toolName = message.tool_name || "tool";
  const toolCallId = message.tool_call_id || "";

  return (
    <details className="disclosure" data-tool-call-id={toolCallId}>
      <summary className="disclosure__summary">
        🛠️ Tool Used: {toolName}
      </summary>
      <div className="disclosure__content">
        <div>
          <div className="tool-detail-row">
            <strong>Tool:</strong> {toolName}
          </div>
          {message.name && (
            <div className="tool-detail-row">
              <strong>Inputs:</strong>
              <pre>{message.name}</pre>
            </div>
          )}
          <div className="tool-detail-row output-row">
            <strong>Output:</strong>
            <pre>{message.content}</pre>
          </div>
        </div>
      </div>
    </details>
  );
}

export default function ChatPage() {
  const params = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isShelfOpen, closeShelf } = useShelf();
  const messagesContainerRef = useRef<HTMLDivElement>(null);
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

  // Sync selectedThreadId from URL parameter - one-way only (URL → state)
  // This ensures state stays consistent with URL, but doesn't override when
  // we're intentionally changing threads via state updates
  useEffect(() => {
    setSelectedThreadId(threadIdParam ?? null);
  }, [threadIdParam]);

  const agentQuery = useQuery<Agent>({
    queryKey: ["agent", agentId],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchAgent(agentId);
    },
    enabled: agentId != null,
  });

  // Fetch chat threads only
  const chatThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", agentId, "chat"],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchThreads(agentId, "chat");
    },
    enabled: agentId != null,
  });

  // Fetch automation threads (scheduled and manual)
  const automationThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", agentId, "automation"],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      // Fetch both scheduled and manual threads
      return Promise.all([
        fetchThreads(agentId, "scheduled"),
        fetchThreads(agentId, "manual"),
      ]).then(([scheduled, manual]) => [...scheduled, ...manual]);
    },
    enabled: agentId != null,
  });

  // Fetch workflows for execution in chat
  const workflowsQuery = useQuery<Workflow[]>({
    queryKey: ["workflows"],
    queryFn: fetchWorkflows,
    staleTime: 60000, // Cache for 1 minute
  });

  const effectiveThreadId = useMemo(() => {
    if (selectedThreadId != null) {
      return selectedThreadId;
    }
    const threads = chatThreadsQuery.data;
    if (threads && threads.length > 0) {
      return threads[0].id;
    }
    return null;
  }, [selectedThreadId, chatThreadsQuery.data]);

  const messagesQuery = useQuery<ThreadMessage[]>({
    queryKey: ["thread-messages", effectiveThreadId],
    queryFn: () => {
      if (effectiveThreadId == null) {
        return Promise.resolve<ThreadMessage[]>([]);
      }
      return fetchThreadMessages(effectiveThreadId);
    },
    enabled: effectiveThreadId != null,
  });

  const sendMutation = useMutation<
    ThreadMessage,
    Error,
    { threadId: number; content: string },
    number
  >({
    mutationFn: async ({ threadId, content }) => {
      const message = await postThreadMessage(threadId, content);
      await runThread(threadId);
      return message;
    },
    onMutate: async ({ threadId, content }) => {
      await queryClient.cancelQueries({ queryKey: ["thread-messages", threadId] });

      const optimisticId = -Date.now();
      // Optimistic message with current time - server will override with its own timestamp
      // Since clocks are usually synced, the displayed time won't change noticeably
      const optimisticMessage = {
        id: optimisticId,
        thread_id: threadId,
        role: "user",
        content,
        sent_at: new Date().toISOString(),
        processed: true,
      } as unknown as ThreadMessage;

      queryClient.setQueryData<ThreadMessage[]>(["thread-messages", threadId], (old) =>
        old ? [...old, optimisticMessage] : [optimisticMessage]
      );

      return optimisticId;
    },
    onError: (_error, variables, optimisticId) => {
      queryClient.setQueryData<ThreadMessage[]>(
        ["thread-messages", variables.threadId],
        (current) => current?.filter((msg) => msg.id !== optimisticId) ?? []
      );
      toast.error("Failed to send message", { duration: 6000 });
    },
    onSuccess: (data, variables, optimisticId) => {
      queryClient.setQueryData<ThreadMessage[]>(
        ["thread-messages", variables.threadId],
        (current) =>
          current?.map((msg) => (msg.id === optimisticId ? data : msg)) ?? [data]
      );
    },
    onSettled: (_data, _error, variables) => {
      queryClient.invalidateQueries({ queryKey: ["thread-messages", variables.threadId] });
    },
  });

  // Workflow execution mutation
  const executeWorkflowMutation = useMutation({
    mutationFn: ({ workflowId }: { workflowId: number }) => startWorkflowExecution(workflowId),
    onSuccess: (result) => {
      toast.success(`Workflow execution started! ID: ${result.execution_id}`);
      setShowWorkflowPanel(false);
      // Send a message to the chat about the workflow execution
      if (effectiveThreadId) {
        sendMutation.mutate({
          threadId: effectiveThreadId,
          content: `🔄 Started workflow execution #${result.execution_id} (Phase: ${result.phase})`,
        });
      }
    },
    onError: (error: Error) => {
      toast.error(`Failed to execute workflow: ${error.message}`);
    },
  });

  const [draft, setDraft] = useState("");

  const renameThreadMutation = useMutation<
    Thread,
    Error,
    { threadId: number; title: string },
    { previousThreads?: Thread[] }
  >({
    mutationFn: ({ threadId, title }) => updateThread(threadId, { title }),
    onMutate: async ({ threadId, title }) => {
      if (agentId == null) {
        return {};
      }
      const queryKey = ["threads", agentId] as const;
      await queryClient.cancelQueries({ queryKey });
      const previousThreads = queryClient.getQueryData<Thread[]>(queryKey);
      queryClient.setQueryData<Thread[]>(queryKey, (old) =>
        old ? old.map((thread) => (thread.id === threadId ? { ...thread, title } : thread)) : old
      );
      return { previousThreads };
    },
    onError: (error, _variables, context) => {
      if (agentId == null) {
        return;
      }
      if (context?.previousThreads) {
        queryClient.setQueryData(["threads", agentId], context.previousThreads);
      }
      toast.error("Failed to rename thread", {
        duration: 6000,
      });
    },
    onSuccess: (updatedThread) => {
      if (agentId != null) {
        queryClient.setQueryData<Thread[]>(["threads", agentId], (old) =>
          old ? old.map((thread) => (thread.id === updatedThread.id ? updatedThread : thread)) : old
        );
      }
      setEditingThreadId(null);
      setEditingTitle("");
    },
    onSettled: (_data, _error, variables) => {
      if (agentId != null) {
        queryClient.invalidateQueries({ queryKey: ["threads", agentId] });
      }
      if (variables) {
        queryClient.invalidateQueries({ queryKey: ["thread-messages", variables.threadId] });
      }
    },
  });

  const isLoading = agentQuery.isLoading || chatThreadsQuery.isLoading || messagesQuery.isLoading;
  const hasError = agentQuery.isError || chatThreadsQuery.isError || messagesQuery.isError;

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

  // Use unified WebSocket hook for real-time chat updates
  const wsQueries = useMemo(() => {
    const queries = [];
    if (agentId != null) {
      queries.push(["threads", agentId, "chat"]);
      queries.push(["threads", agentId, "automation"]);
    }
    if (effectiveThreadId != null) {
      queries.push(["thread-messages", effectiveThreadId]);
    }
    return queries;
  }, [agentId, effectiveThreadId]);

  useWebSocket(agentId != null, {
    includeAuth: true,
    invalidateQueries: wsQueries,
  });

  useEffect(() => {
    if (agentId != null && effectiveThreadId != null) {
      navigate(`/chat/${agentId}/${effectiveThreadId}`, { replace: true });
    } else if (agentId != null) {
      navigate(`/chat/${agentId}`, { replace: true });
    }
  }, [agentId, effectiveThreadId, navigate]);

  const chatThreads = useMemo(() => {
    const list = chatThreadsQuery.data ?? [];
    // Sort threads by updated_at (newest first), falling back to created_at
    return [...list].sort((a, b) => {
      const aTime = a.updated_at || a.created_at;
      const bTime = b.updated_at || b.created_at;
      return bTime.localeCompare(aTime);
    });
  }, [chatThreadsQuery.data]);

  const automationThreads = useMemo(() => {
    const list = automationThreadsQuery.data ?? [];
    // Sort by created_at (newest first)
    return [...list].sort((a, b) => {
      const aTime = a.created_at;
      const bTime = b.created_at;
      return bTime.localeCompare(aTime);
    });
  }, [automationThreadsQuery.data]);

  const messages = messagesQuery.data ?? [];

  const agent = agentQuery.data;

  // Group tool messages by parent_id for rendering under assistant messages
  const toolMessagesByParent = useMemo(() => {
    const map = new Map<number, ThreadMessage[]>();
    messages
      .filter(m => m.role === "tool" && m.parent_id != null)
      .forEach(msg => {
        const list = map.get(msg.parent_id!) || [];
        list.push(msg);
        map.set(msg.parent_id!, list);
      });
    return map;
  }, [messages]);

  // Get orphaned tool messages (no parent_id)
  const orphanedToolMessages = useMemo(() => {
    return messages.filter(m => m.role === "tool" && m.parent_id == null);
  }, [messages]);

  const handleSelectThread = (thread: Thread) => {
    setSelectedThreadId(thread.id);
    navigate(`/chat/${agentId}/${thread.id}`, { replace: true });
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
    } catch (error) {
      // Error handling is now done in the mutation's onError callback
    }
  };

  const handleCancelEdit = () => {
    setEditingThreadId(null);
    setEditingTitle("");
  };

  const ensureActiveThread = useCallback(async () => {
    if (effectiveThreadId != null) {
      return effectiveThreadId;
    }

    if (chatThreads.length > 0) {
      const firstThreadId = chatThreads[0].id;
      setSelectedThreadId(firstThreadId);
      return firstThreadId;
    }

    if (agentId == null) {
      return null;
    }

    try {
      const thread = await createThread(agentId, "Primary Thread");
      await queryClient.invalidateQueries({ queryKey: ["threads", agentId, "chat"] });
      setSelectedThreadId(thread.id);
      return thread.id;
    } catch (error) {
      toast.error("Failed to create thread", {
        duration: 6000,
      });
      return null;
    }
  }, [agentId, effectiveThreadId, queryClient, chatThreads]);

  const handleSend = async (evt: FormEvent) => {
    evt.preventDefault();
    const trimmed = draft.trim();
    if (!trimmed) {
      return;
    }
    const threadId = await ensureActiveThread();
    if (threadId == null) {
      return;
    }
    setDraft("");
    try {
      await sendMutation.mutateAsync({ threadId, content: trimmed });
    } catch (error) {
      // Error handling is now done in the mutation's onError callback
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
  };

  // Auto-create and select a default thread on component mount if none exists
  useEffect(() => {
    const initializeThread = async () => {
      if (agentId == null || selectedThreadId != null || chatThreads.length > 0) {
        return;
      }

      // Prevent concurrent creation attempts
      if (creatingThreadRef.current) return;
      creatingThreadRef.current = true;

      try {
        // Auto-create a default thread for the agent
        const thread = await createThread(agentId, "Thread 1");
        await queryClient.invalidateQueries({ queryKey: ["threads", agentId, "chat"] });
        setSelectedThreadId(thread.id);
      } catch (error) {
        // Silently fail - user can create thread manually if needed
      } finally {
        creatingThreadRef.current = false;
      }
    };

    if (agentId != null && selectedThreadId == null && chatThreads.length === 0) {
      initializeThread();
    }
  }, [agentId, selectedThreadId, chatThreads.length, queryClient]);

  // Scroll to bottom when messages change
  useEffect(() => {
    if (messagesContainerRef.current) {
      messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
    }
  }, [messages]);

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

  if (isLoading) {
    return <div>Loading chat…</div>;
  }

  if (hasError) {
    return <div>Unable to load chat view.</div>;
  }

  const handleCreateThread = async () => {
    if (agentId == null) return;
    // Auto-generate thread name based on the count of existing threads
    const threadCount = chatThreads.length + 1;
    const title = `Thread ${threadCount}`;
    try {
      const thread = await createThread(agentId, title);
      queryClient.invalidateQueries({ queryKey: ["threads", agentId, "chat"] });
      setSelectedThreadId(thread.id);
      navigate(`/chat/${agentId}/${thread.id}`, { replace: true });
    } catch (error) {
      toast.error("Failed to create thread", { duration: 6000 });
    }
  };

  return (
    <>
      <div id="chat-view-container" className="chat-view-container">
      <header className="chat-header">
        <button
          type="button"
          className="back-button"
          onClick={() => navigate("/dashboard")}
        >
          ←
        </button>
        {agent?.id != null && (
          <button
            type="button"
            data-testid={`chat-agent-${agent.id}`}
            onClick={() => {
              if (effectiveThreadId != null) {
                navigate(`/chat/${agent.id}/${effectiveThreadId}`, { replace: true });
              } else {
                navigate(`/chat/${agent.id}`, { replace: true });
              }
            }}
            aria-hidden="true"
            tabIndex={-1}
            style={{
              position: "absolute",
              width: 1,
              height: 1,
              opacity: 0,
              pointerEvents: "auto",
              overflow: "hidden",
            }}
          >
            {agent.name}
          </button>
        )}
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
        <aside className={clsx("thread-sidebar", { active: isShelfOpen })}>
          <div className="sidebar-header">
            <h3>Threads</h3>
            <button
              type="button"
              className="new-thread-btn"
              data-testid="new-thread-btn"
              onClick={handleCreateThread}
            >
              New Thread
            </button>
          </div>
          <div className="thread-list">
            {chatThreads.map((thread) => {
              const threadMessages = messages.filter(m => m.thread_id === thread.id);
              const lastMessage = threadMessages[threadMessages.length - 1];
              const messagePreview = lastMessage
                ? truncateText(lastMessage.content, 50)
                : "No messages";

              return (
                <div
                  key={thread.id}
                  className={clsx("thread-item", { selected: thread.id === effectiveThreadId })}
                  data-testid={`thread-row-${thread.id}`}
                  data-id={thread.id}
                  data-thread-id={thread.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => handleSelectThread(thread)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      handleSelectThread(thread);
                    }
                  }}
                >
                  {editingThreadId === thread.id ? (
                    <div className="thread-edit-form" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="text"
                        value={editingTitle}
                        onChange={(e) => setEditingTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            handleSaveThreadTitle(thread.id);
                          } else if (e.key === "Escape") {
                            handleCancelEdit();
                          }
                        }}
                        autoFocus
                        className="thread-title-input"
                        disabled={renameThreadMutation.isPending}
                      />
                    </div>
                  ) : (
                    <>
                      <div className="thread-item-title">{thread.title}</div>
                      <div className="thread-item-time">
                        {formatTimestamp(thread.updated_at || thread.created_at)}
                      </div>
                      <button
                        type="button"
                        className="thread-edit-button"
                        data-testid={`edit-thread-${thread.id}`}
                        onClick={(e) => handleEditThreadTitle(thread, e)}
                        aria-label="Edit thread title"
                        title="Edit thread title"
                        disabled={renameThreadMutation.isPending}
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
            <div className="automation-history">
              <h4 className="automation-history-title">Automation Runs</h4>
              <div className="automation-runs-list">
                {automationThreads.map((thread) => (
                  <div
                    key={thread.id}
                    className={clsx("automation-run-item", {
                      selected: thread.id === effectiveThreadId,
                    })}
                    data-testid={`automation-run-${thread.id}`}
                    data-id={thread.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => handleSelectThread(thread)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        handleSelectThread(thread);
                      }
                    }}
                  >
                    <div className="automation-run-title">{thread.title}</div>
                    <div className="automation-run-time">
                      {formatTimestamp(thread.created_at)}
                    </div>
                    <div className="automation-run-type">
                      <span className={`run-badge run-badge-${thread.thread_type}`}>
                        {thread.thread_type === "scheduled" ? "🔄 Scheduled" : "▶️ Manual"}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </aside>

        <section className="conversation-area">
          <div className="messages-container" data-testid="messages-container" ref={messagesContainerRef}>
            {messages
              .filter(msg => msg.role !== "system" && msg.role !== "tool")
              .map((msg, index) => {
                const isLastUserMessage = msg.role === "user" && index === messages.length - 1;
                const toolMessages = toolMessagesByParent.get(msg.id);

                // Skip rendering empty assistant messages (they only have tool calls)
                if (msg.role === "assistant" && msg.content.trim() === "") {
                  return (
                    <div key={msg.id}>
                      {toolMessages?.map(toolMsg => (
                        <ToolMessage key={toolMsg.id} message={toolMsg} />
                      ))}
                    </div>
                  );
                }

                return (
                  <div key={msg.id}>
                    <div className="chat-row">
                      <article
                        className={clsx("message", {
                          "user-message": msg.role === "user",
                          "assistant-message": msg.role === "assistant",
                        })}
                        data-testid={isLastUserMessage ? "chat-message" : undefined}
                        data-role={`chat-message-${msg.role}`}
                      >
                        <div className="message-content preserve-whitespace">{msg.content}</div>
                        <div className="message-footer">
                          <div className="message-time">{formatTimestamp(msg.created_at)}</div>
                          <div className="message-actions">
                            <button
                              type="button"
                              className="message-action-btn"
                              onClick={() => handleCopyMessage(msg)}
                              title="Copy message"
                            >
                              📋
                            </button>
                          </div>
                        </div>
                      </article>
                    </div>
                    {msg.role === "assistant" && toolMessages?.map(toolMsg => (
                      <ToolMessage key={toolMsg.id} message={toolMsg} />
                    ))}
                  </div>
                );
              })}
            {orphanedToolMessages.map(toolMsg => (
              <ToolMessage key={toolMsg.id} message={toolMsg} />
            ))}
            {messages.length === 0 && (
              <p className="thread-list-empty">No messages yet.</p>
            )}
          </div>
        </section>
      </div>

      {/* Scrim overlay when thread sidebar is open on mobile */}
      <div
        className={clsx("thread-scrim", { "thread-scrim--visible": isShelfOpen })}
        onClick={closeShelf}
      />

      {/* Enhanced Chat Input Area */}
      <div className="chat-input-wrapper">
        {/* Chat Tools Bar */}
        <div className="chat-tools">
          <button
            type="button"
            className="tool-btn"
            onClick={() => setShowWorkflowPanel(!showWorkflowPanel)}
            title="Execute Workflow"
          >
            🔧 Workflows
          </button>
          <button
            type="button"
            className="tool-btn"
            onClick={handleExportChat}
            disabled={messages.length === 0}
            title="Export Chat History"
          >
            📄 Export
          </button>
        </div>

        <form className="chat-input-area" onSubmit={handleSend}>
          <input
            type="text"
            value={draft}
            onChange={(evt) => setDraft(evt.target.value)}
            placeholder={effectiveThreadId ? "Type your message..." : "Select a thread to start chatting"}
            className="chat-input"
            data-testid="chat-input"
            disabled={!effectiveThreadId}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend(e as React.FormEvent);
              }
            }}
          />
          <button
            type="submit"
            className={clsx("send-button", { disabled: !effectiveThreadId })}
            disabled={sendMutation.isPending || !draft.trim() || !effectiveThreadId}
            data-testid="send-message-btn"
          >
            {sendMutation.isPending ? "Sending…" : "Send"}
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
                onClick={() => setShowWorkflowPanel(false)}
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
                      onChange={(e) => setSelectedWorkflow(Number(e.target.value) || null)}
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
                    onClick={handleExecuteWorkflow}
                    disabled={!selectedWorkflow || executeWorkflowMutation.isPending}
                  >
                    {executeWorkflowMutation.isPending ? "Executing..." : "▶️ Execute"}
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
