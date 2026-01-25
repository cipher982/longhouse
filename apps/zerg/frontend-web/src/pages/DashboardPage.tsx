import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactElement } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  fetchDashboardSnapshot,
  runAgent,
  updateAgent,
  fetchModels,
  type AgentRun,
  type AgentSummary,
  type DashboardSnapshot,
  type ModelConfig,
} from "../services/api";
import { buildUrl } from "../services/api";
import { ConnectionStatus, useWebSocket } from "../lib/useWebSocket";
import { useAuth } from "../lib/auth";
import { PlusIcon } from "../components/icons";
import AgentSettingsDrawer from "../components/agent-settings/AgentSettingsDrawer";
import UsageWidget from "../components/UsageWidget";
import type { WebSocketMessage } from "../generated/ws-messages";
import {
  Button,
  Table,
  SectionHeader,
  EmptyState,
  Spinner
} from "../components/ui";
import { useConfirm } from "../components/confirm";
import { AgentTableRow } from "./dashboard/AgentTableRow";
import { sortAgents, loadSortConfig, persistSortConfig, type SortKey, type SortConfig, type AgentRunsState } from "./dashboard/sorting";

// App logo (served from public folder)
const appLogo = "/Gemini_Generated_Image_klhmhfklhmhfklhm-removebg-preview.png";

type Scope = "my" | "all";

const RUNS_LIMIT = 50;

export default function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();
  const confirm = useConfirm();
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
    data: modelsData,
  } = useQuery<ModelConfig[]>({
    queryKey: ["models"],
    queryFn: fetchModels,
    staleTime: 1000 * 60 * 60, // 1 hour
  });

  const defaultModel = useMemo(() => {
    return modelsData?.find((m) => m.is_default)?.id || "gpt-5.2";
  }, [modelsData]);

  const {
    data: dashboardData,
    isLoading,
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

  // Readiness Contract (see src/lib/readiness-contract.ts):
  // - data-ready="true": Page is INTERACTIVE (can click, type)
  // - data-screenshot-ready="true": Content loaded for marketing captures
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute('data-ready', 'true');
      // Dashboard is screenshot-ready as soon as it's interactive
      // (agents table is visible even if empty)
      document.body.setAttribute('data-screenshot-ready', 'true');
    }
    return () => {
      document.body.removeAttribute('data-ready');
      document.body.removeAttribute('data-screenshot-ready');
    };
  }, [isLoading]);

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

  // Cleanup effect - runs only on unmount to unsubscribe from all agents
  /* eslint-disable react-hooks/exhaustive-deps -- Intentional: cleanup reads current values at unmount time */
  useEffect(() => {
    // Capture refs for cleanup (ESLint wants this pattern)
    const pendingSubscriptions = pendingSubscriptionsRef.current;
    const subscribedAgentIds = subscribedAgentIdsRef.current;
    const sendMessage = sendMessageRef.current;
    const msgId = generateMessageId; // Capture for cleanup

    return () => {
      // Clear pending subscription timeouts
      pendingSubscriptions.forEach((pending) => {
        clearTimeout(pending.timeoutId);
      });
      pendingSubscriptions.clear();

      if (subscribedAgentIds.size === 0) {
        return;
      }
      const topics = Array.from(subscribedAgentIds).map((id) => `agent:${id}`);
      sendMessage?.({
        type: "unsubscribe",
        topics,
        message_id: msgId(),
      });
      subscribedAgentIds.clear();
    };
  }, []);
  /* eslint-enable react-hooks/exhaustive-deps */

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
          "Idempotency-Key": key,
        },
        credentials: 'include', // Cookie auth
        body: JSON.stringify({
          system_instructions: "You are a helpful AI assistant.",
          task_instructions: "Complete the given task.",
          model: defaultModel,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to create agent: ${response.status}`);
      }

      return response.json();
    },
    onSuccess: () => {
      // WebSocket will deliver the agent with real name
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
      idempotencyKeyRef.current = null; // Reset for next creation
    },
  });

  // Delete agent mutation
  const deleteAgentMutation = useMutation({
    mutationFn: async (agentId: number) => {
      const response = await fetch(buildUrl(`/agents/${agentId}`), {
        method: "DELETE",
        credentials: 'include', // Cookie auth
      });
      if (!response.ok) throw new Error("Delete failed");
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
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
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
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

  const sortedAgents = useMemo(() => {
    return sortAgents(agents, runsByAgent, sortConfig);
  }, [agents, runsByAgent, sortConfig]);

  if (isLoading) {
    return (
      <div id="dashboard-container" className="dashboard-page">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading fiches..."
          description="Fetching your autonomous workforce."
        />
      </div>
    );
  }

  if (error) {
    const message = error instanceof Error ? error.message : "Failed to load agents";
    return (
      <div id="dashboard-container" className="dashboard-page">
        <EmptyState
          variant="error"
          title="Error loading dashboard"
          description={message}
          action={
            <Button onClick={() => queryClient.invalidateQueries({ queryKey: dashboardQueryKey })}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }

  const includeOwner = scope === "all";
  const emptyColspan = includeOwner ? 8 : 7;

  return (
    <div id="dashboard-container" className="dashboard-page">
      <SectionHeader
        title={scope === "all" ? "All fiches" : "My fiches"}
        description="Monitor and manage your autonomous commis."
        actions={
          <div className="button-container">
            <div className="scope-wrapper">
              <label className="scope-toggle">
                <input
                  type="checkbox"
                  id="dashboard-scope-toggle"
                  data-testid="dashboard-scope-toggle"
                  aria-label="Toggle between my fiches and all fiches"
                  checked={scope === "all"}
                  onChange={(e) => {
                    const newScope = e.target.checked ? "all" : "my";
                    setScope(newScope);
                  }}
                />
                <span className="slider"></span>
              </label>
            </div>
            <Button
              variant="primary"
              onClick={() => createAgentMutation.mutate()}
              disabled={createAgentMutation.isPending}
              data-testid="create-agent-btn"
            >
              {createAgentMutation.isPending ? (
                <Spinner size="sm" />
              ) : (
                <>
                  <PlusIcon />
                  Create Fiche
                </>
              )}
            </Button>
          </div>
        }
      />

      <div className="dashboard-content">
        {/* LLM Usage Widget */}
        <UsageWidget />

        <Table>
          <Table.Header>
            {renderHeaderCell("Name", "name", sortConfig, handleSort)}
            {includeOwner && renderHeaderCell("Owner", "owner", sortConfig, handleSort, false)}
            {renderHeaderCell("Status", "status", sortConfig, handleSort)}
            {renderHeaderCell("Created", "created_at", sortConfig, handleSort)}
            {renderHeaderCell("Last Run", "last_run", sortConfig, handleSort)}
            {renderHeaderCell("Next Run", "next_run", sortConfig, handleSort)}
            {renderHeaderCell("Success Rate", "success", sortConfig, handleSort)}
            <Table.Cell isHeader className="actions-header">
              Actions
            </Table.Cell>
          </Table.Header>
          <Table.Body id="agents-table-body">
            {sortedAgents.map((agent) => (
              <AgentTableRow
                key={agent.id}
                agent={agent}
                runs={runsByAgent[agent.id] || []}
                includeOwner={includeOwner}
                isExpanded={expandedAgentId === agent.id}
                isRunHistoryExpanded={expandedRunHistory.has(agent.id)}
                isPendingRun={runAgentMutation.isPending && runAgentMutation.variables === agent.id}
                runsDataLoading={runsDataLoading}
                editingAgentId={editingAgentId}
                editingName={editingName}
                onToggleRow={toggleAgentRow}
                onToggleRunHistory={toggleRunHistory}
                onRunAgent={handleRunAgent}
                onChatAgent={handleChatAgent}
                onDebugAgent={handleDebugAgent}
                onDeleteAgent={handleDeleteAgent}
                onStartEditingName={startEditingName}
                onSaveNameAndExit={saveNameAndExit}
                onCancelEditing={cancelEditing}
                onEditingNameChange={setEditingName}
                onRunActionsClick={dispatchDashboardEvent.bind(null, "run-actions")}
              />
            ))}
            {sortedAgents.length === 0 && (
              <Table.Row>
                <Table.Cell isHeader colSpan={emptyColspan}>
                  <EmptyState
                    icon={<img src={appLogo} alt="Swarmlet Logo" className="dashboard-empty-logo" />}
                    title="No fiches found"
                    description="Click 'Create Fiche' to get started."
                  />
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table>
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

  async function handleDeleteAgent(event: ReactMouseEvent<HTMLButtonElement>, agentId: number, name: string) {
    event.stopPropagation();
    const confirmed = await confirm({
      title: `Delete fiche "${name}"?`,
      message: 'This action cannot be undone. All associated data will be permanently removed.',
      confirmLabel: 'Delete',
      cancelLabel: 'Keep',
      variant: 'danger',
    });
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
) => ReactElement;

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
