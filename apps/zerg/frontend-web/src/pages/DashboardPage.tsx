import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  fetchDashboardSnapshot,
  runAgent,
  updateAgent,
  type AgentRun,
  type AgentSummary,
  type DashboardSnapshot,
} from "../services/api";
import { buildUrl } from "../services/api";
import { ConnectionStatus, useWebSocket } from "../lib/useWebSocket";
import { useAuth } from "../lib/auth";
import { MessageCircleIcon, PlayIcon, SettingsIcon, TrashIcon } from "../components/icons";
import AgentSettingsDrawer from "../components/agent-settings/AgentSettingsDrawer";
import type { WebSocketMessage } from "../generated/ws-messages";

type Scope = "my" | "all";
type SortKey = "name" | "status" | "last_run" | "next_run" | "success";

type SortConfig = {
  key: SortKey;
  ascending: boolean;
};

type AgentRunsState = Record<number, AgentRun[]>;

type LegacyAgentRow = {
  agent: AgentSummary;
  lastRunDisplay: string;
  nextRunDisplay: string;
};

const STATUS_ORDER: Record<string, number> = {
  running: 0,
  processing: 1,
  idle: 2,
  error: 3,
};

const NBSP = "\u00A0";

const STORAGE_KEY_SORT = "dashboard_sort_key";
const STORAGE_KEY_ASC = "dashboard_sort_asc";
const RUNS_LIMIT = 50;

export default function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();
  const [scope, setScope] = useState<Scope>("my");
  const [sortConfig, setSortConfig] = useState<SortConfig>(() => loadSortConfig());
  const [expandedAgentId, setExpandedAgentId] = useState<number | null>(null);
  const dashboardQueryKey = useMemo(() => ["dashboard", scope, RUNS_LIMIT] as const, [scope]);
  const [expandedRunHistory, setExpandedRunHistory] = useState<Set<number>>(new Set());
  const [settingsAgentId, setSettingsAgentId] = useState<number | null>(null);
  const [editingAgentId, setEditingAgentId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState<string>("");

  // WebSocket state - must be declared before useQuery to avoid reference errors
  const subscribedAgentIdsRef = useRef<Set<number>>(new Set());
  const [wsReconnectToken, setWsReconnectToken] = useState(0);
  const sendMessageRef = useRef<((message: any) => void) | null>(null);
  const messageIdCounterRef = useRef(0);

  // Track pending subscriptions to handle confirmations and timeouts
  // Don't mark as subscribed until we get subscribe_ack to enable automatic retry
  const pendingSubscriptionsRef = useRef<Map<string, { topics: string[]; timeoutId: number; agentIds: number[] }>>(new Map());

  // Generate unique message IDs to prevent collision
  const generateMessageId = useCallback(() => {
    messageIdCounterRef.current += 1;
    return `dashboard-${Date.now()}-${messageIdCounterRef.current}`;
  }, []);

  const applyDashboardUpdate = useCallback(
    (updater: (current: DashboardSnapshot) => DashboardSnapshot) => {
      queryClient.setQueryData<DashboardSnapshot>(dashboardQueryKey, (current) => {
        if (!current) {
          return current;
        }
        return updater(current);
      });
    },
    [dashboardQueryKey, queryClient]
  );

  // WebSocket message handler must be defined before useWebSocket hook
  const handleWebSocketMessage = useCallback(
    (message: WebSocketMessage | { type: string; topic?: string; data?: any; message_id?: string }) => {
      if (!message || typeof message !== "object") {
        return;
      }

      if (message.type === "subscribe_ack" || message.type === "subscribe_error") {
        const messageData = (message as any).data || message;
        const messageId = typeof messageData.message_id === "string" ? messageData.message_id : "";
        if (messageId && pendingSubscriptionsRef.current.has(messageId)) {
          const pending = pendingSubscriptionsRef.current.get(messageId);
          if (pending) {
            clearTimeout(pending.timeoutId);
            pendingSubscriptionsRef.current.delete(messageId);

            if (message.type === "subscribe_ack") {
              pending.agentIds.forEach((agentId) => {
                subscribedAgentIdsRef.current.add(agentId);
              });
            } else {
              console.error("[WS] Subscription failed for topics:", pending.topics);
              setWsReconnectToken((token) => token + 1);
            }
          }
        }
        return;
      }

      const topic = typeof message.topic === "string" ? message.topic : "";
      if (!topic.startsWith("agent:")) {
        return;
      }

      const [, agentIdRaw] = topic.split(":");
      const agentId = Number.parseInt(agentIdRaw ?? "", 10);
      if (!Number.isFinite(agentId)) {
        return;
      }

      const dataPayload =
        typeof message.data === "object" && message.data !== null ? (message.data as Record<string, unknown>) : {};
      const eventType = message.type;

      if (eventType === "agent_state" || eventType === "agent_updated") {
        const validStatuses = ["idle", "running", "processing", "error"] as const;
        const statusValue =
          typeof dataPayload.status === "string" && validStatuses.includes(dataPayload.status as (typeof validStatuses)[number])
            ? (dataPayload.status as AgentSummary["status"])
            : undefined;
        const lastRunAtValue = typeof dataPayload.last_run_at === "string" ? dataPayload.last_run_at : undefined;
        const nextRunAtValue = typeof dataPayload.next_run_at === "string" ? dataPayload.next_run_at : undefined;
        const lastErrorValue =
          dataPayload.last_error === null || typeof dataPayload.last_error === "string"
            ? (dataPayload.last_error as string | null)
            : undefined;

        applyDashboardUpdate((current) => {
          let changed = false;
          const nextAgents = current.agents.map((agent) => {
            if (agent.id !== agentId) {
              return agent;
            }

            const nextAgent: AgentSummary = {
              ...agent,
              status: statusValue ?? agent.status,
              last_run_at: lastRunAtValue ?? agent.last_run_at,
              next_run_at: nextRunAtValue ?? agent.next_run_at,
              last_error: lastErrorValue !== undefined ? lastErrorValue : agent.last_error,
            };

            if (
              nextAgent.status !== agent.status ||
              nextAgent.last_run_at !== agent.last_run_at ||
              nextAgent.next_run_at !== agent.next_run_at ||
              nextAgent.last_error !== agent.last_error
            ) {
              changed = true;
              return nextAgent;
            }
            return agent;
          });

          if (!changed) {
            return current;
          }

          return {
            ...current,
            agents: nextAgents,
          };
        });

        return;
      }

      if (eventType === "run_update") {
        const runIdCandidate = dataPayload.id ?? dataPayload.run_id;
        const runId = typeof runIdCandidate === "number" ? runIdCandidate : null;
        if (runId == null) {
          return;
        }

        const threadId =
          typeof dataPayload.thread_id === "number" ? (dataPayload.thread_id as number) : undefined;

        applyDashboardUpdate((current) => {
          const runsBundles = current.runs.slice();
          let bundleIndex = runsBundles.findIndex((bundle) => bundle.agentId === agentId);
          let runsChanged = false;

          if (bundleIndex === -1) {
            runsBundles.push({ agentId, runs: [] });
            bundleIndex = runsBundles.length - 1;
            runsChanged = true;
          }

          const targetBundle = runsBundles[bundleIndex];
          const existingRuns = targetBundle.runs ?? [];
          const existingIndex = existingRuns.findIndex((run) => run.id === runId);
          let nextRuns = existingRuns;

          if (existingIndex === -1) {
            if (threadId === undefined) {
              return current;
            }

            const newRun: AgentRun = {
              id: runId,
              agent_id: agentId,
              thread_id: threadId,
              status:
                typeof dataPayload.status === "string"
                  ? (dataPayload.status as AgentRun["status"])
                  : "running",
              trigger:
                typeof dataPayload.trigger === "string"
                  ? (dataPayload.trigger as AgentRun["trigger"])
                  : "manual",
              started_at: typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : null,
              finished_at: typeof dataPayload.finished_at === "string" ? (dataPayload.finished_at as string) : null,
              duration_ms: typeof dataPayload.duration_ms === "number" ? (dataPayload.duration_ms as number) : null,
              total_tokens: typeof dataPayload.total_tokens === "number" ? (dataPayload.total_tokens as number) : null,
              total_cost_usd:
                typeof dataPayload.total_cost_usd === "number" ? (dataPayload.total_cost_usd as number) : null,
              error:
                dataPayload.error === undefined
                  ? null
                  : (dataPayload.error as string | null) ?? null,
            };

            nextRuns = [newRun, ...existingRuns];
            if (nextRuns.length > current.runsLimit) {
              nextRuns = nextRuns.slice(0, current.runsLimit);
            }
            runsChanged = true;
          } else {
            const previousRun = existingRuns[existingIndex];
            const updatedRun: AgentRun = {
              ...previousRun,
              status:
                typeof dataPayload.status === "string"
                  ? (dataPayload.status as AgentRun["status"])
                  : previousRun.status,
              started_at:
                typeof dataPayload.started_at === "string"
                  ? (dataPayload.started_at as AgentRun["started_at"])
                  : previousRun.started_at,
              finished_at:
                typeof dataPayload.finished_at === "string"
                  ? (dataPayload.finished_at as AgentRun["finished_at"])
                  : previousRun.finished_at,
              duration_ms:
                typeof dataPayload.duration_ms === "number"
                  ? (dataPayload.duration_ms as AgentRun["duration_ms"])
                  : previousRun.duration_ms,
              total_tokens:
                typeof dataPayload.total_tokens === "number"
                  ? (dataPayload.total_tokens as AgentRun["total_tokens"])
                  : previousRun.total_tokens,
              total_cost_usd:
                typeof dataPayload.total_cost_usd === "number"
                  ? (dataPayload.total_cost_usd as AgentRun["total_cost_usd"])
                  : previousRun.total_cost_usd,
              error:
                dataPayload.error === undefined
                  ? previousRun.error
                  : ((dataPayload.error as string | null) ?? null),
            };

            const hasRunDiff =
              updatedRun.status !== previousRun.status ||
              updatedRun.started_at !== previousRun.started_at ||
              updatedRun.finished_at !== previousRun.finished_at ||
              updatedRun.duration_ms !== previousRun.duration_ms ||
              updatedRun.total_tokens !== previousRun.total_tokens ||
              updatedRun.total_cost_usd !== previousRun.total_cost_usd ||
              updatedRun.error !== previousRun.error;

            if (hasRunDiff) {
              nextRuns = [...existingRuns];
              nextRuns[existingIndex] = updatedRun;
              runsChanged = true;
            }
          }

          if (runsChanged) {
            runsBundles[bundleIndex] = {
              agentId,
              runs: nextRuns,
            };
          }

          let agentsChanged = false;
          const updatedAgents = current.agents.map((agent) => {
            if (agent.id !== agentId) {
              return agent;
            }

            const statusValue =
              typeof dataPayload.status === "string"
                ? (dataPayload.status as AgentSummary["status"])
                : agent.status;
            const lastRunValue =
              typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : agent.last_run_at;

            if (statusValue === agent.status && lastRunValue === agent.last_run_at) {
              return agent;
            }

            agentsChanged = true;
            return {
              ...agent,
              status: statusValue,
              last_run_at: lastRunValue,
            };
          });

          if (!runsChanged && !agentsChanged) {
            return current;
          }

          return {
            ...current,
            agents: agentsChanged ? updatedAgents : current.agents,
            runs: runsChanged ? runsBundles : current.runs,
          };
        });
      }
    },
    [applyDashboardUpdate]
  );

  const { connectionStatus, sendMessage } = useWebSocket(isAuthenticated, {
    onMessage: handleWebSocketMessage,
    onConnect: () => {
      subscribedAgentIdsRef.current.clear();
      // Clear any pending subscriptions from previous connection
      pendingSubscriptionsRef.current.forEach((pending) => {
        clearTimeout(pending.timeoutId);
      });
      pendingSubscriptionsRef.current.clear();
      setWsReconnectToken((token) => token + 1);
    },
  });

  const {
    data: dashboardData,
    isLoading,
    isFetching,
    error,
  } = useQuery<DashboardSnapshot>({
    queryKey: dashboardQueryKey,
    queryFn: () => fetchDashboardSnapshot({ scope, runsLimit: RUNS_LIMIT }),
    refetchInterval: connectionStatus === ConnectionStatus.CONNECTED ? false : 2000,
  });

  const agents: AgentSummary[] = useMemo(() => dashboardData?.agents ?? [], [dashboardData]);

  const runsByAgent: AgentRunsState = useMemo(() => {
    if (!dashboardData) {
      return {};
    }

    const lookup: AgentRunsState = {};
    for (const bundle of dashboardData.runs) {
      lookup[bundle.agentId] = bundle.runs;
    }

    for (const agent of dashboardData.agents) {
      if (!lookup[agent.id]) {
        lookup[agent.id] = [];
      }
    }

    return lookup;
  }, [dashboardData]);

  const runsDataLoading = isLoading && !dashboardData;

  // Keep sendMessage ref up-to-date for stable cleanup
  useEffect(() => {
    sendMessageRef.current = sendMessage;
  }, [sendMessage]);

  // Mutation for starting an agent run (hybrid: optimistic + WebSocket)
  const runAgentMutation = useMutation({
    mutationFn: runAgent,
    onMutate: async (agentId: number) => {
      await queryClient.cancelQueries({ queryKey: dashboardQueryKey });

      const previousSnapshot = queryClient.getQueryData<DashboardSnapshot>(dashboardQueryKey);

      queryClient.setQueryData<DashboardSnapshot>(dashboardQueryKey, (current) => {
        if (!current) {
          return current;
        }

        return {
          ...current,
          agents: current.agents.map((agent) =>
            agent.id === agentId ? { ...agent, status: "running" as const } : agent
          ),
        };
      });

      return { previousSnapshot };
    },
    onError: (err: Error, agentId: number, context) => {
      if (context?.previousSnapshot) {
        queryClient.setQueryData(dashboardQueryKey, context.previousSnapshot);
      }
      console.error("Failed to run agent:", err);
    },
    onSettled: (_, __, agentId) => {
      dispatchDashboardEvent("run", agentId);
    },
  });

  useEffect(() => {
    // Persist sort preferences whenever they change.
    persistSortConfig(sortConfig);
  }, [sortConfig]);

  useEffect(() => {
    if (!error) {
      return;
    }

    if (error instanceof Error && error.message.includes("(403)")) {
      setScope("my");
    }
  }, [error]);

  useEffect(() => {
    if (expandedAgentId === null) {
      return;
    }
    if (agents.some((agent) => agent.id === expandedAgentId)) {
      return;
    }
    setExpandedAgentId(null);
  }, [agents, expandedAgentId]);

  // Use unified WebSocket hook for real-time updates
  // Only connect when authenticated to avoid auth failure spam
  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    if (connectionStatus !== ConnectionStatus.CONNECTED) {
      return;
    }

    const activeIds = new Set(agents.map((agent) => agent.id));

    // Find agents that need subscription (not currently subscribed AND not pending)
    const pendingAgentIds = new Set<number>();
    pendingSubscriptionsRef.current.forEach((pending) => {
      pending.agentIds.forEach((id) => pendingAgentIds.add(id));
    });

    const topicsToSubscribe: string[] = [];
    const agentIdsToSubscribe: number[] = [];
    for (const id of activeIds) {
      if (!subscribedAgentIdsRef.current.has(id) && !pendingAgentIds.has(id)) {
        topicsToSubscribe.push(`agent:${id}`);
        agentIdsToSubscribe.push(id);
      }
    }

    const topicsToUnsubscribe: string[] = [];
    for (const id of Array.from(subscribedAgentIdsRef.current)) {
      if (!activeIds.has(id)) {
        subscribedAgentIdsRef.current.delete(id);
        topicsToUnsubscribe.push(`agent:${id}`);
      }
    }

    if (topicsToSubscribe.length > 0) {
      const messageId = generateMessageId();

      // Set timeout for subscription confirmation (5 seconds)
      const timeoutId = window.setTimeout(() => {
        if (pendingSubscriptionsRef.current.has(messageId)) {
          console.warn("[WS] Subscription timeout for topics:", topicsToSubscribe);
          pendingSubscriptionsRef.current.delete(messageId);
          // Don't mark as subscribed - effect will retry on next render
          // Force retry by incrementing reconnect token
          setWsReconnectToken((token) => token + 1);
        }
      }, 5000);

      // Track pending subscription (don't mark as subscribed yet)
      pendingSubscriptionsRef.current.set(messageId, {
        topics: topicsToSubscribe,
        timeoutId,
        agentIds: agentIdsToSubscribe
      });

      sendMessageRef.current?.({
        type: "subscribe",
        topics: topicsToSubscribe,
        message_id: messageId,
      });
    }

    if (topicsToUnsubscribe.length > 0) {
      sendMessageRef.current?.({
        type: "unsubscribe",
        topics: topicsToUnsubscribe,
        message_id: generateMessageId(),
      });
    }
  }, [agents, connectionStatus, isAuthenticated, wsReconnectToken, generateMessageId]);

  useEffect(() => {
    if (isAuthenticated) {
      return;
    }

    if (subscribedAgentIdsRef.current.size === 0) {
      return;
    }

    const topics = Array.from(subscribedAgentIdsRef.current).map((id) => `agent:${id}`);
    sendMessageRef.current?.({
      type: "unsubscribe",
      topics,
      message_id: generateMessageId(),
    });
    subscribedAgentIdsRef.current.clear();
  }, [isAuthenticated, generateMessageId]);

  useEffect(() => {
    return () => {
      // Clear pending subscription timeouts
      pendingSubscriptionsRef.current.forEach((pending) => {
        clearTimeout(pending.timeoutId);
      });
      pendingSubscriptionsRef.current.clear();

      if (subscribedAgentIdsRef.current.size === 0) {
        return;
      }
      const topics = Array.from(subscribedAgentIdsRef.current).map((id) => `agent:${id}`);
      // Use ref to avoid cleanup re-registration on every render
      sendMessageRef.current?.({
        type: "unsubscribe",
        topics,
        message_id: generateMessageId(),
      });
      subscribedAgentIdsRef.current.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Intentionally empty - cleanup runs only on unmount, uses refs for stable access // Empty deps - cleanup only runs on unmount

  // Generate idempotency key per mutation to prevent double-creates
  const idempotencyKeyRef = useRef<string | null>(null);

  const createAgentMutation = useMutation({
    mutationFn: async () => {
      // Generate fresh key for each create attempt
      const key = `create-agent-${Date.now()}-${Math.random()}`;
      idempotencyKeyRef.current = key;

      // Backend auto-generates name as "Agent #<id>"
      const response = await fetch(buildUrl("/agents"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("zerg_jwt")}`,
          "Idempotency-Key": key,
        },
        body: JSON.stringify({
          system_instructions: "You are a helpful AI assistant.",
          task_instructions: "Complete the given task.",
          model: "gpt-4o",
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to create agent: ${response.status}`);
      }

      return response.json();
    },
    onSuccess: () => {
      // WebSocket will deliver the agent with real name
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      idempotencyKeyRef.current = null; // Reset for next creation
    },
  });

  // Delete agent mutation
  const deleteAgentMutation = useMutation({
    mutationFn: async (agentId: number) => {
      const response = await fetch(buildUrl(`/agents/${agentId}`), {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${localStorage.getItem("zerg_jwt")}`,
        },
      });
      if (!response.ok) throw new Error("Delete failed");
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  // Inline name editing handlers
  function startEditingName(agentId: number, currentName: string) {
    setEditingAgentId(agentId);
    setEditingName(currentName);
  }

  async function saveNameAndExit(agentId: number) {
    if (!editingName.trim()) {
      // Don't allow empty names
      return;
    }

    try {
      await updateAgent(agentId, { name: editingName });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    } catch (error) {
      console.error("Failed to rename:", error);
    }

    setEditingAgentId(null);
    setEditingName("");
  }

  function cancelEditing() {
    setEditingAgentId(null);
    setEditingName("");
  }

  const sortedRows: LegacyAgentRow[] = useMemo(() => {
    return sortAgents(agents, runsByAgent, sortConfig).map((agent) => ({
      agent,
      lastRunDisplay: formatDateTimeShort(agent.last_run_at ?? null),
      nextRunDisplay: formatDateTimeShort(agent.next_run_at ?? null),
    }));
  }, [agents, runsByAgent, sortConfig]);

  if (isLoading) {
    return (
      <div id="dashboard-container" className="dashboard-container">
        <div id="dashboard" className="dashboard">
          <div>Loading agents…</div>
        </div>
      </div>
    );
  }

  if (error) {
    const message = error instanceof Error ? error.message : "Failed to load agents";
    return (
      <div id="dashboard-container" className="dashboard-container">
        <div id="dashboard" className="dashboard">
          <div>{message}</div>
        </div>
      </div>
    );
  }

  const includeOwner = scope === "all";
  const emptyColspan = includeOwner ? 7 : 6;

  return (
    <div id="dashboard-container" className="dashboard-container">
      <div id="dashboard" className="dashboard">
        <div className="dashboard-header">
          <div className="scope-wrapper">
            <span className="scope-text-label" id="scope-text">
              {scope === "all" ? "All agents" : "My agents"}
            </span>
            <label className="scope-toggle">
              <input
                type="checkbox"
                id="dashboard-scope-toggle"
                data-testid="dashboard-scope-toggle"
                checked={scope === "all"}
                onChange={(e) => {
                  const newScope = e.target.checked ? "all" : "my";
                  setScope(newScope);
                }}
              />
              <span className="slider"></span>
            </label>
          </div>
          <div className="button-container">
            <button
              id="create-agent-button"
              type="button"
              className={`create-agent-button${createAgentMutation.isPending ? " loading" : ""}`}
              data-testid="create-agent-btn"
              onClick={() => createAgentMutation.mutate()}
              disabled={createAgentMutation.isPending}
            >
              {createAgentMutation.isPending ? <span className="spinner" /> : "Create Agent"}
            </button>
          </div>
        </div>

        <table id="agents-table" className="agents-table">
          <thead>
            <tr>
              {renderHeaderCell("Name", "name", sortConfig, handleSort)}
              {includeOwner && renderHeaderCell("Owner", "owner", sortConfig, handleSort, false)}
              {renderHeaderCell("Status", "status", sortConfig, handleSort)}
              {renderHeaderCell("Last Run", "last_run", sortConfig, handleSort)}
              {renderHeaderCell("Next Run", "next_run", sortConfig, handleSort)}
              {renderHeaderCell("Success Rate", "success", sortConfig, handleSort)}
              <th
                scope="col"
                className="actions-header"
                data-column="actions"
                onClick={() => handleSort("name")}
                role="button"
                tabIndex={0}
              >
                Actions
              </th>
            </tr>
          </thead>
          <tbody id="agents-table-body">
            {sortedRows.map(({ agent, lastRunDisplay, nextRunDisplay }) => {
              const runs = runsByAgent[agent.id];
              const isExpanded = expandedAgentId === agent.id;
              const isRunHistoryExpanded = expandedRunHistory.has(agent.id);
              const successStats = computeSuccessStats(runs);
              const lastRunIndicator = determineLastRunIndicator(runs);
              const isRunning = agent.status === "running";
              // Check if this specific agent is being mutated
              const isPendingRun = runAgentMutation.isPending && runAgentMutation.variables === agent.id;

              return (
                <Fragment key={agent.id}>
                  <tr
                    data-agent-id={agent.id}
                    aria-expanded={isExpanded ? "true" : "false"}
                    className={agent.status === "error" ? "error-row" : undefined}
                    tabIndex={0}
                    onClick={() => toggleAgentRow(agent.id)}
                    onKeyDown={(event) => handleRowKeyDown(event, agent.id)}
                  >
                    <td data-label="Name" onClick={(e) => e.stopPropagation()}>
                      {editingAgentId === agent.id ? (
                        <input
                          className="inline-edit-input"
                          value={editingName}
                          onChange={(e) => setEditingName(e.target.value)}
                          onBlur={() => saveNameAndExit(agent.id)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.stopPropagation();
                              saveNameAndExit(agent.id);
                            }
                            if (e.key === "Escape") {
                              e.stopPropagation();
                              cancelEditing();
                            }
                          }}
                          onClick={(e) => e.stopPropagation()}
                          autoFocus
                        />
                      ) : (
                        <span
                          className="editable-name"
                          onClick={() => startEditingName(agent.id, agent.name)}
                          title="Click to rename"
                        >
                          {agent.name}
                        </span>
                      )}
                    </td>
                    {includeOwner && (
                      <td className="owner-cell" data-label="Owner">
                        {renderOwnerCell(agent)}
                      </td>
                    )}
                    <td data-label="Status">
                      <span className={`status-indicator status-${agent.status.toLowerCase()}`}>
                        {formatStatus(agent.status)}
                      </span>
                      {agent.last_error && agent.last_error.trim() && (
                        <span className="info-icon" title={agent.last_error}>
                          ℹ
                        </span>
                      )}
                      {lastRunIndicator !== null && (
                        <span
                          className={lastRunIndicator ? "last-run-indicator last-run-success" : "last-run-indicator last-run-failure"}
                        >
                          {lastRunIndicator ? " (Last: ✓)" : " (Last: ✗)"}
                        </span>
                      )}
                    </td>
                    <td data-label="Last Run">{lastRunDisplay}</td>
                    <td data-label="Next Run">{nextRunDisplay}</td>
                    <td data-label="Success Rate">{successStats.display}</td>
                    <td className="actions-cell" data-label="Actions">
                      <div className="actions-cell-inner">
                        <button
                          type="button"
                          className={`action-btn run-btn${isRunning || isPendingRun ? " disabled" : ""}`}
                          data-testid={`run-agent-${agent.id}`}
                          disabled={isRunning || isPendingRun}
                          title={isRunning ? "Agent is already running" : "Run Agent"}
                          aria-label={isRunning ? "Agent is already running" : "Run Agent"}
                          onClick={(event) => handleRunAgent(event, agent.id, agent.status)}
                        >
                          <PlayIcon />
                        </button>
                        <button
                          type="button"
                          className="action-btn chat-btn"
                          data-testid={`chat-agent-${agent.id}`}
                          title="Chat with Agent"
                          aria-label="Chat with Agent"
                          onClick={(event) => handleChatAgent(event, agent.id, agent.name)}
                        >
                          <MessageCircleIcon />
                        </button>
                        <button
                          type="button"
                          className="action-btn debug-btn"
                          data-testid={`debug-agent-${agent.id}`}
                          title="Debug / Info"
                          aria-label="Debug / Info"
                          onClick={(event) => handleDebugAgent(event, agent.id)}
                        >
                          <SettingsIcon />
                        </button>
                        <button
                          type="button"
                          className="action-btn delete-btn"
                          data-testid={`delete-agent-${agent.id}`}
                          title="Delete Agent"
                          aria-label="Delete Agent"
                          onClick={(event) => handleDeleteAgent(event, agent.id, agent.name)}
                        >
                          <TrashIcon />
                        </button>
                      </div>
                    </td>
                  </tr>
                  {isExpanded && (
                    <tr className="agent-detail-row" key={`detail-${agent.id}`}>
                      <td colSpan={emptyColspan}>
                        <div className="agent-detail-container">
                          {runsDataLoading && <span>Loading run history...</span>}
                          {!runsDataLoading && runs && runs.length === 0 && (
                            <span>No runs recorded yet.</span>
                          )}
                          {!runsDataLoading && runs && runs.length > 0 && (
                            <>
                              <table className="run-history-table">
                                <thead>
                                  <tr>
                                    <th>Status</th>
                                    <th>Started</th>
                                    <th>Duration</th>
                                    <th>Trigger</th>
                                    <th>Tokens</th>
                                    <th>Cost</th>
                                    <th />
                                  </tr>
                                </thead>
                                <tbody>
                                  {runs
                                    .slice(0, isRunHistoryExpanded ? runs.length : Math.min(runs.length, 5))
                                    .map((run) => (
                                      <tr key={run.id}>
                                        <td>{formatRunStatusIcon(run.status)}</td>
                                        <td>{formatDateTimeShort(run.started_at ?? null)}</td>
                                        <td>{formatDuration(run.duration_ms)}</td>
                                        <td>{capitaliseFirst(run.trigger)}</td>
                                        <td>{formatTokens(run.total_tokens)}</td>
                                        <td>{formatCost(run.total_cost_usd)}</td>
                                        <td className="run-kebab-cell">
                                          <span
                                            className="kebab-menu-btn"
                                            role="button"
                                            tabIndex={0}
                                            onClick={(event) => {
                                              event.preventDefault();
                                              event.stopPropagation();
                                              dispatchDashboardEvent("run-actions", agent.id, run.id);
                                            }}
                                            onKeyDown={(event) => {
                                              if (event.key === "Enter" || event.key === " ") {
                                                event.preventDefault();
                                                event.stopPropagation();
                                                dispatchDashboardEvent("run-actions", agent.id, run.id);
                                              }
                                            }}
                                          >
                                            ⋮
                                          </span>
                                        </td>
                                      </tr>
                                    ))}
                                </tbody>
                              </table>
                              {runs.length > 5 && (
                                <a
                                  href="#"
                                  className="run-toggle-link"
                                  aria-expanded={isRunHistoryExpanded ? "true" : "false"}
                                  onClick={(event) => {
                                    event.preventDefault();
                                    toggleRunHistory(agent.id);
                                  }}
                                >
                                  {isRunHistoryExpanded ? "Show less" : `Show all (${runs.length})`}
                                </a>
                              )}
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {sortedRows.length === 0 && (
              <tr>
                <td colSpan={emptyColspan}>
                  <div className="empty-state">
                    <div className="empty-state-illustration">🤖</div>
                    <p className="empty-state-text">
                      No agents found. Click 'Create Agent' to get started.
                    </p>
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {settingsAgentId != null && (
        <AgentSettingsDrawer
          agentId={settingsAgentId}
          isOpen={settingsAgentId != null}
          onClose={() => setSettingsAgentId(null)}
        />
      )}
    </div>
  );

  function toggleAgentRow(agentId: number) {
    setExpandedAgentId((prev) => (prev === agentId ? null : agentId));
  }

  function toggleRunHistory(agentId: number) {
    setExpandedRunHistory((prev) => {
      const next = new Set(prev);
      if (next.has(agentId)) {
        next.delete(agentId);
      } else {
        next.clear();
        next.add(agentId);
      }
      return next;
    });
  }

  function handleSort(key: SortKey) {
    setSortConfig((prev) => {
      if (prev.key === key) {
        return { key, ascending: !prev.ascending };
      }
      return { key, ascending: true };
    });
  }

  function handleRowKeyDown(event: ReactKeyboardEvent<HTMLTableRowElement>, agentId: number) {
    const key = event.key;
    if (key === "Enter") {
      event.preventDefault();
      toggleAgentRow(agentId);
      return;
    }

    if (key !== "ArrowDown" && key !== "ArrowUp") {
      return;
    }

    event.preventDefault();
    const current = event.currentTarget;
    const tbody = current.closest("tbody");
    if (!tbody) {
      return;
    }
    const rows = Array.from(tbody.querySelectorAll<HTMLTableRowElement>("tr[data-agent-id]"));
    const index = rows.indexOf(current);
    if (index === -1) {
      return;
    }
    const nextIndex = key === "ArrowDown" ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1);
    rows[nextIndex]?.focus();
  }

  function handleRunAgent(event: ReactMouseEvent<HTMLButtonElement>, agentId: number, status: string) {
    event.stopPropagation();
    // Don't run if already running
    if (status === "running") {
      return;
    }
    // Use the optimistic mutation
    runAgentMutation.mutate(agentId);
  }

  function handleChatAgent(event: ReactMouseEvent<HTMLButtonElement>, agentId: number, agentName: string) {
    event.stopPropagation();
    navigate(`/agent/${agentId}/thread/?name=${encodeURIComponent(agentName)}`);
  }

  function handleDebugAgent(event: ReactMouseEvent<HTMLButtonElement>, agentId: number) {
    event.stopPropagation();
    setSettingsAgentId(agentId);
  }

  function handleDeleteAgent(event: ReactMouseEvent<HTMLButtonElement>, agentId: number, name: string) {
    event.stopPropagation();
    const confirmed = typeof window === "undefined" || window.confirm(`Delete agent ${name}?`);
    if (!confirmed) {
      return;
    }
    deleteAgentMutation.mutate(agentId);
  }
}

type HeaderRenderer = (
  label: string,
  sortKey: SortKey | "owner",
  sortConfig: SortConfig,
  onSort: (key: SortKey) => void,
  sortable?: boolean
) => JSX.Element;

const renderHeaderCell: HeaderRenderer = (label, sortKey, sortConfig, onSort, sortable = true) => {
  const dataColumn = label.toLowerCase().replace(/\s+/g, "_");
  const effectiveKey = sortKey === "owner" ? "name" : sortKey;
  const isActive = sortable && sortConfig.key === effectiveKey;
  const arrow = sortConfig.ascending ? "▲" : "▼";

  return (
    <th
      scope="col"
      data-column={dataColumn}
      onClick={sortable ? () => onSort(effectiveKey as SortKey) : undefined}
      role={sortable ? "button" : undefined}
      tabIndex={sortable ? 0 : undefined}
    >
      {label}
      {isActive && <span className="sort-indicator">{arrow}</span>}
    </th>
  );
};

function renderOwnerCell(agent: AgentSummary) {
  if (!agent.owner) {
    return <span>-</span>;
  }

  const label = agent.owner.display_name?.trim() || agent.owner.email;
  if (!label) {
    return <span>-</span>;
  }

  return (
    <div className="owner-wrapper">
      {agent.owner.avatar_url && <img src={agent.owner.avatar_url} alt="" className="owner-avatar" aria-hidden="true" />}
      <span>{label}</span>
    </div>
  );
}

function sortAgents(agents: AgentSummary[], runsByAgent: AgentRunsState, sortConfig: SortConfig): AgentSummary[] {
  const sorted = [...agents];
  sorted.sort((left, right) => {
    const comparison = compareAgents(left, right, runsByAgent, sortConfig.key);
    if (comparison !== 0) {
      return sortConfig.ascending ? comparison : -comparison;
    }
    const fallback = left.name.toLowerCase().localeCompare(right.name.toLowerCase());
    return sortConfig.ascending ? fallback : -fallback;
  });
  return sorted;
}

function compareAgents(
  left: AgentSummary,
  right: AgentSummary,
  runsByAgent: AgentRunsState,
  sortKey: SortKey
): number {
  switch (sortKey) {
    case "name":
      return left.name.toLowerCase().localeCompare(right.name.toLowerCase());
    case "status":
      return (STATUS_ORDER[left.status] ?? 99) - (STATUS_ORDER[right.status] ?? 99);
    case "last_run":
      return formatDateTimeShort(left.last_run_at ?? null).localeCompare(
        formatDateTimeShort(right.last_run_at ?? null)
      );
    case "next_run":
      return formatDateTimeShort(left.next_run_at ?? null).localeCompare(
        formatDateTimeShort(right.next_run_at ?? null)
      );
    case "success": {
      const leftStats = computeSuccessStats(runsByAgent[left.id]);
      const rightStats = computeSuccessStats(runsByAgent[right.id]);
      if (leftStats.rate === rightStats.rate) {
        return leftStats.count - rightStats.count;
      }
      return leftStats.rate - rightStats.rate;
    }
    default:
      return 0;
  }
}

function computeSuccessStats(runs?: AgentRun[]): { display: string; rate: number; count: number } {
  if (!runs || runs.length === 0) {
    return { display: "0.0% (0)", rate: 0, count: 0 };
  }

  const successCount = runs.filter((run) => run.status === "success").length;
  const successRate = runs.length === 0 ? 0 : (successCount / runs.length) * 100;
  return {
    display: `${successRate.toFixed(1)}% (${runs.length})`,
    rate: successRate,
    count: runs.length,
  };
}

function determineLastRunIndicator(runs?: AgentRun[]): boolean | null {
  if (!runs || runs.length === 0) {
    return null;
  }
  const status = runs[0]?.status;
  if (status === "success") {
    return true;
  }
  if (status === "failed") {
    return false;
  }
  return null;
}

function formatDateTimeShort(iso: string | null | undefined): string {
  if (!iso) {
    return "-";
  }

  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }

  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const day = String(date.getUTCDate()).padStart(2, "0");
  const hours = String(date.getUTCHours()).padStart(2, "0");
  const minutes = String(date.getUTCMinutes()).padStart(2, "0");
  return `${year}-${month}-${day}${NBSP}${hours}:${minutes}`;
}

function formatStatus(status: string): string {
  switch (status) {
    case "running":
      return "● Running";
    case "processing":
      return "⏳ Processing";
    case "error":
      return "⚠ Error";
    case "idle":
    default:
      return "○ Idle";
  }
}

function formatDuration(durationMs?: number | null): string {
  if (!durationMs) {
    return "-";
  }
  const secondsTotal = Math.floor(durationMs / 1000);
  const minutes = Math.floor(secondsTotal / 60);
  const seconds = secondsTotal % 60;
  if (minutes > 0) {
    return `${minutes} m ${String(seconds).padStart(2, "0")} s`;
  }
  return `${seconds} s`;
}

function capitaliseFirst(value: string): string {
  if (!value) {
    return "";
  }
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function formatTokens(tokens?: number | null): string {
  if (tokens === null || tokens === undefined) {
    return "—";
  }
  return tokens.toString();
}

function formatCost(cost?: number | null): string {
  if (cost === null || cost === undefined) {
    return "—";
  }
  if (cost >= 0.1) {
    return `$${cost.toFixed(2)}`;
  }
  if (cost >= 0.01) {
    return `$${cost.toFixed(3)}`;
  }
  return `$${cost.toFixed(4)}`;
}

function formatRunStatusIcon(status: AgentRun["status"]): string {
  switch (status) {
    case "running":
      return "▶";
    case "success":
      return "✔";
    case "failed":
      return "✖";
    default:
      return "●";
  }
}

function loadSortConfig(): SortConfig {
  if (typeof window === "undefined") {
    return { key: "name", ascending: true };
  }

  const storedKey = window.localStorage.getItem(STORAGE_KEY_SORT) ?? "name";
  const storedAsc = window.localStorage.getItem(STORAGE_KEY_ASC);

  const keyMap: Record<string, SortKey> = {
    name: "name",
    status: "status",
    last_run: "last_run",
    next_run: "next_run",
    success: "success",
  };

  const key = keyMap[storedKey] ?? "name";
  const ascending = storedAsc === null ? true : storedAsc !== "0";
  return { key, ascending };
}

function persistSortConfig(config: SortConfig) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(STORAGE_KEY_SORT, config.key);
  window.localStorage.setItem(STORAGE_KEY_ASC, config.ascending ? "1" : "0");
}

type DashboardEventType = "run" | "edit" | "debug" | "delete" | "run-actions";

function dispatchDashboardEvent(type: DashboardEventType, agentId: number, runId?: number) {
  if (typeof window === "undefined") {
    return;
  }
  const event = new CustomEvent("dashboard:event", {
    detail: {
      type,
      agentId,
      runId,
    },
  });
  window.dispatchEvent(event);
}
