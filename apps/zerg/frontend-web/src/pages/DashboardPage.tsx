import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactElement } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  createAutomation as createAutomationRecord,
  deleteAutomation as deleteAutomationRecord,
  fetchAutomationOverview,
  runAutomation as runAutomationTask,
  updateAutomation as updateAutomationRecord,
  fetchModels,
  type Run,
  type AutomationSummary,
  type AutomationOverviewSnapshot,
  type ModelConfig,
} from "../services/api";
import { ConnectionStatus, createEnvelope, useWebSocket } from "../lib/useWebSocket";
import { DEFAULT_TEXT_MODEL } from "../oikos/core/model-config";
import { useAuth } from "../lib/auth";
import { PlusIcon } from "../components/icons";
import AutomationSettingsDrawer from "../components/automation-settings/AutomationSettingsDrawer";
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
import { AutomationTableRow } from "./dashboard/AutomationTableRow";
import { sortAutomations, loadSortConfig, persistSortConfig, type SortKey, type SortConfig, type AutomationRunsState } from "./dashboard/sorting";
import { applyRunUpdate, applyAutomationStateUpdate } from "./dashboard/websocketHandlers";

// App logo (served from public folder)
const appLogo = "/Gemini_Generated_Image_klhmhfklhmhfklhm-removebg-preview.png";

type Scope = "my" | "all";

const RUNS_LIMIT = 50;
const AUTOMATION_TOPIC_PREFIX = "automation:";
const LEGACY_FICHE_TOPIC_PREFIX = "fiche:";

function parseDashboardTopic(topic: string): number | null {
  if (!(topic.startsWith(AUTOMATION_TOPIC_PREFIX) || topic.startsWith(LEGACY_FICHE_TOPIC_PREFIX))) {
    return null;
  }

  const [, automationIdRaw] = topic.split(":");
  const automationId = Number.parseInt(automationIdRaw ?? "", 10);
  return Number.isFinite(automationId) ? automationId : null;
}

function isAutomationLifecycleEvent(eventType: string): boolean {
  return (
    eventType === "automation_state" ||
    eventType === "automation_updated" ||
    eventType === "fiche_state" ||
    eventType === "fiche_updated"
  );
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();
  const confirm = useConfirm();
  const [scope, setScope] = useState<Scope>("my");
  const [sortConfig, setSortConfig] = useState<SortConfig>(() => loadSortConfig());
  const [expandedAutomationId, setExpandedAutomationId] = useState<number | null>(null);
  const dashboardQueryKey = useMemo(() => ["dashboard", scope, RUNS_LIMIT] as const, [scope]);
  const [expandedRunHistory, setExpandedRunHistory] = useState<Set<number>>(new Set());
  const [settingsAutomationId, setSettingsAutomationId] = useState<number | null>(null);
  const [editingAutomationId, setEditingAutomationId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState<string>("");

  // WebSocket state - must be declared before useQuery to avoid reference errors
  const subscribedAutomationIdsRef = useRef<Set<number>>(new Set());
  const [wsReconnectToken, setWsReconnectToken] = useState(0);
  const sendMessageRef = useRef<((message: any) => void) | null>(null);
  const messageIdCounterRef = useRef(0);

  // Track pending subscriptions to handle confirmations and timeouts
  // Don't mark as subscribed until we get subscribe_ack to enable automatic retry
  const pendingSubscriptionsRef = useRef<Map<string, { topics: string[]; timeoutId: number; automationIds: number[] }>>(new Map());

  // Generate unique message IDs to prevent collision
  const generateMessageId = useCallback(() => {
    messageIdCounterRef.current += 1;
    return `dashboard-${Date.now()}-${messageIdCounterRef.current}`;
  }, []);

  const applyDashboardUpdate = useCallback(
    (updater: (current: AutomationOverviewSnapshot) => AutomationOverviewSnapshot) => {
      queryClient.setQueryData<AutomationOverviewSnapshot>(dashboardQueryKey, (current) => {
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
              pending.automationIds.forEach((automationId) => {
                subscribedAutomationIdsRef.current.add(automationId);
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
      const automationId = parseDashboardTopic(topic);
      if (automationId == null) {
        return;
      }

      const dataPayload =
        typeof message.data === "object" && message.data !== null ? (message.data as Record<string, unknown>) : {};
      const eventType = message.type;

      if (isAutomationLifecycleEvent(eventType)) {
        applyDashboardUpdate((current) => applyAutomationStateUpdate(current, automationId, dataPayload));
        return;
      }

      if (eventType === "run_update") {
        applyDashboardUpdate((current) => applyRunUpdate(current, automationId, dataPayload));
        return;
      }
    },
    [applyDashboardUpdate]
  );

  const { connectionStatus, sendMessage } = useWebSocket(isAuthenticated, {
    onMessage: handleWebSocketMessage,
    onConnect: () => {
      subscribedAutomationIdsRef.current.clear();
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
    return modelsData?.find((m) => m.is_default)?.id || DEFAULT_TEXT_MODEL;
  }, [modelsData]);

  const {
    data: dashboardData,
    isLoading,
    error,
  } = useQuery<AutomationOverviewSnapshot>({
    queryKey: dashboardQueryKey,
    queryFn: () => fetchAutomationOverview({ scope, runsLimit: RUNS_LIMIT }),
    refetchInterval: connectionStatus === ConnectionStatus.CONNECTED ? false : 2000,
  });

  const automations: AutomationSummary[] = useMemo(() => dashboardData?.automations ?? [], [dashboardData]);

  const runsByAutomation: AutomationRunsState = useMemo(() => {
    if (!dashboardData) {
      return {};
    }

    const lookup: AutomationRunsState = {};
    for (const bundle of dashboardData.runs) {
      lookup[bundle.automationId] = bundle.runs;
    }

    for (const automation of dashboardData.automations) {
      if (!lookup[automation.id]) {
        lookup[automation.id] = [];
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
      // Dashboard is screenshot-ready as soon as it's interactive.
      // The automations table is visible even if empty.
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

  // Mutation for starting an automation run (hybrid: optimistic + WebSocket)
  const startRunMutation = useMutation({
    mutationFn: runAutomationTask,
    onMutate: async (automationId: number) => {
      await queryClient.cancelQueries({ queryKey: dashboardQueryKey });

      const previousSnapshot = queryClient.getQueryData<AutomationOverviewSnapshot>(dashboardQueryKey);

      queryClient.setQueryData<AutomationOverviewSnapshot>(dashboardQueryKey, (current) => {
        if (!current) {
          return current;
        }

        return {
          ...current,
          automations: current.automations.map((automation) =>
            automation.id === automationId ? { ...automation, status: "running" as const } : automation
          ),
        };
      });

      return { previousSnapshot };
    },
    onError: (err: Error, automationId: number, context) => {
      if (context?.previousSnapshot) {
        queryClient.setQueryData(dashboardQueryKey, context.previousSnapshot);
      }
      console.error("Failed to start run:", err);
    },
    onSettled: (_, __, automationId) => {
      dispatchDashboardEvent("run", automationId);
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
    if (expandedAutomationId === null) {
      return;
    }
    if (automations.some((automation) => automation.id === expandedAutomationId)) {
      return;
    }
    setExpandedAutomationId(null);
  }, [automations, expandedAutomationId]);

  // Use unified WebSocket hook for real-time updates
  // Only connect when authenticated to avoid auth failure spam
  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    if (connectionStatus !== ConnectionStatus.CONNECTED) {
      return;
    }

    const activeIds = new Set(automations.map((automation) => automation.id));

    // Find automations that need subscription (not currently subscribed AND not pending).
    const pendingAutomationIds = new Set<number>();
    pendingSubscriptionsRef.current.forEach((pending) => {
      pending.automationIds.forEach((id) => pendingAutomationIds.add(id));
    });

    const topicsToSubscribe: string[] = [];
    const automationIdsToSubscribe: number[] = [];
    for (const id of activeIds) {
      if (!subscribedAutomationIdsRef.current.has(id) && !pendingAutomationIds.has(id)) {
        topicsToSubscribe.push(`${AUTOMATION_TOPIC_PREFIX}${id}`);
        automationIdsToSubscribe.push(id);
      }
    }

    const topicsToUnsubscribe: string[] = [];
    for (const id of Array.from(subscribedAutomationIdsRef.current)) {
      if (!activeIds.has(id)) {
        subscribedAutomationIdsRef.current.delete(id);
        topicsToUnsubscribe.push(`${AUTOMATION_TOPIC_PREFIX}${id}`);
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
        automationIds: automationIdsToSubscribe
      });

      sendMessageRef.current?.(
        createEnvelope("subscribe", "system", { topics: topicsToSubscribe, message_id: messageId }, messageId),
      );
    }

    if (topicsToUnsubscribe.length > 0) {
      const unsubMsgId = generateMessageId();
      sendMessageRef.current?.(
        createEnvelope("unsubscribe", "system", { topics: topicsToUnsubscribe, message_id: unsubMsgId }, unsubMsgId),
      );
    }
  }, [automations, connectionStatus, isAuthenticated, wsReconnectToken, generateMessageId]);

  useEffect(() => {
    if (isAuthenticated) {
      return;
    }

    if (subscribedAutomationIdsRef.current.size === 0) {
      return;
    }

    const topics = Array.from(subscribedAutomationIdsRef.current).map((id) => `${AUTOMATION_TOPIC_PREFIX}${id}`);
    const unsubId = generateMessageId();
    sendMessageRef.current?.(
      createEnvelope("unsubscribe", "system", { topics, message_id: unsubId }, unsubId),
    );
    subscribedAutomationIdsRef.current.clear();
  }, [isAuthenticated, generateMessageId]);

  // Cleanup effect - runs only on unmount to unsubscribe from all automations
  /* eslint-disable react-hooks/exhaustive-deps -- Intentional: cleanup reads current values at unmount time */
  useEffect(() => {
    // Capture refs for cleanup (ESLint wants this pattern)
    const pendingSubscriptions = pendingSubscriptionsRef.current;
    const subscribedAutomationIds = subscribedAutomationIdsRef.current;
    const sendMessage = sendMessageRef.current;
    const msgId = generateMessageId; // Capture for cleanup

    return () => {
      // Clear pending subscription timeouts
      pendingSubscriptions.forEach((pending) => {
        clearTimeout(pending.timeoutId);
      });
      pendingSubscriptions.clear();

      if (subscribedAutomationIds.size === 0) {
        return;
      }
      const topics = Array.from(subscribedAutomationIds).map((id) => `${AUTOMATION_TOPIC_PREFIX}${id}`);
      const cleanupMsgId = msgId();
      sendMessage?.(
        createEnvelope("unsubscribe", "system", { topics, message_id: cleanupMsgId }, cleanupMsgId),
      );
      subscribedAutomationIds.clear();
    };
  }, []);
  /* eslint-enable react-hooks/exhaustive-deps */

  // Generate idempotency key per mutation to prevent double-creates
  const idempotencyKeyRef = useRef<string | null>(null);

  const createAutomationMutation = useMutation({
    mutationFn: async () => {
      // Generate fresh key for each create attempt
      const key = `create-automation-${Date.now()}-${Math.random()}`;
      idempotencyKeyRef.current = key;

      // Backend auto-generates a placeholder name.
      return createAutomationRecord(
        {
          system_instructions: "You are a helpful AI assistant.",
          task_instructions: "Complete the given task.",
          model: defaultModel,
        },
        {
          idempotencyKey: key,
        }
      );
    },
    onSuccess: () => {
      // WebSocket will deliver the automation with its final name.
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
      idempotencyKeyRef.current = null; // Reset for next creation
    },
  });

  // Delete automation mutation
  const deleteAutomationMutation = useMutation({
    mutationFn: deleteAutomationRecord,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
    },
  });

  // Inline name editing handlers
  function startEditingName(automationId: number, currentName: string) {
    setEditingAutomationId(automationId);
    setEditingName(currentName);
  }

  async function saveNameAndExit(automationId: number) {
    if (!editingName.trim()) {
      // Don't allow empty names
      return;
    }

    try {
      await updateAutomationRecord(automationId, { name: editingName });
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
    } catch (error) {
      console.error("Failed to rename:", error);
    }

    setEditingAutomationId(null);
    setEditingName("");
  }

  function cancelEditing() {
    setEditingAutomationId(null);
    setEditingName("");
  }

  const sortedAutomations = useMemo(() => {
    return sortAutomations(automations, runsByAutomation, sortConfig);
  }, [automations, runsByAutomation, sortConfig]);

  if (isLoading) {
    return (
      <div id="dashboard-container" className="dashboard-page">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading automations..."
          description="Fetching your automations."
        />
      </div>
    );
  }

  if (error) {
    const message = error instanceof Error ? error.message : "Failed to load automations";
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
        className="dashboard-hero"
        title={scope === "all" ? "All automations" : "My automations"}
        description="Monitor and manage your automations."
        actions={
          <div className="dashboard-actions">
            <div className="dashboard-actions__stats">
              <UsageWidget />
            </div>
            <div className="dashboard-actions__controls">
              <div className="scope-wrapper">
                <label className="scope-toggle">
                  <input
                    type="checkbox"
                    id="dashboard-scope-toggle"
                    data-testid="dashboard-scope-toggle"
                    aria-label="Toggle between my automations and all automations"
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
                onClick={() => createAutomationMutation.mutate()}
                disabled={createAutomationMutation.isPending}
                data-testid="create-automation-btn"
              >
                {createAutomationMutation.isPending ? (
                  <Spinner size="sm" />
                ) : (
                  <>
                    <PlusIcon />
                    Create Automation
                  </>
                )}
              </Button>
            </div>
          </div>
        }
      />

      <div className="dashboard-content">
        <Table className="automations-table">
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
          <Table.Body id="automations-table-body">
            {sortedAutomations.map((automation) => (
              <AutomationTableRow
                key={automation.id}
                automation={automation}
                runs={runsByAutomation[automation.id] || []}
                includeOwner={includeOwner}
                isExpanded={expandedAutomationId === automation.id}
                isRunHistoryExpanded={expandedRunHistory.has(automation.id)}
                isPendingRun={startRunMutation.isPending && startRunMutation.variables === automation.id}
                runsDataLoading={runsDataLoading}
                editingAutomationId={editingAutomationId}
                editingName={editingName}
                onToggleRow={toggleAutomationRow}
                onToggleRunHistory={toggleRunHistory}
                onRunAutomation={handleStartRun}
                onChatAutomation={handleChatAutomation}
                onDebugAutomation={handleDebugAutomation}
                onDeleteAutomation={handleDeleteAutomation}
                onStartEditingName={startEditingName}
                onSaveNameAndExit={saveNameAndExit}
                onCancelEditing={cancelEditing}
                onEditingNameChange={setEditingName}
                onRunActionsClick={dispatchDashboardEvent.bind(null, "run-actions")}
              />
            ))}
            {sortedAutomations.length === 0 && (
              <Table.Row className="automations-empty-row">
                <Table.Cell colSpan={emptyColspan} className="automations-empty-cell">
                  <EmptyState
                    icon={<img src={appLogo} alt="Longhouse Logo" className="dashboard-empty-logo" />}
                    title="No automations found"
                    description="Click 'Create Automation' to get started."
                  />
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table>
      </div>
      {settingsAutomationId != null && (
        <AutomationSettingsDrawer
          automationId={settingsAutomationId}
          isOpen={settingsAutomationId != null}
          onClose={() => setSettingsAutomationId(null)}
        />
      )}
    </div>
  );

  function toggleAutomationRow(automationId: number) {
    setExpandedAutomationId((prev) => (prev === automationId ? null : automationId));
  }

  function toggleRunHistory(automationId: number) {
    setExpandedRunHistory((prev) => {
      const next = new Set(prev);
      if (next.has(automationId)) {
        next.delete(automationId);
      } else {
        next.clear();
        next.add(automationId);
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

  function handleStartRun(event: ReactMouseEvent<HTMLButtonElement>, automationId: number, status: string) {
    event.stopPropagation();
    // Don't run if already running
    if (status === "running") {
      return;
    }
    // Use the optimistic mutation
    startRunMutation.mutate(automationId);
  }

  function handleChatAutomation(
    event: ReactMouseEvent<HTMLButtonElement>,
    automationId: number,
    automationName: string
  ) {
    event.stopPropagation();
    navigate(`/fiche/${automationId}/thread/?name=${encodeURIComponent(automationName)}`);
  }

  function handleDebugAutomation(event: ReactMouseEvent<HTMLButtonElement>, automationId: number) {
    event.stopPropagation();
    setSettingsAutomationId(automationId);
  }

  async function handleDeleteAutomation(event: ReactMouseEvent<HTMLButtonElement>, automationId: number, name: string) {
    event.stopPropagation();
    const confirmed = await confirm({
      title: `Delete automation "${name}"?`,
      message: 'This action cannot be undone. All associated data will be permanently removed.',
      confirmLabel: 'Delete',
      cancelLabel: 'Keep',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }
    deleteAutomationMutation.mutate(automationId);
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

function dispatchDashboardEvent(type: DashboardEventType, automationId: number, runId?: number) {
  if (typeof window === "undefined") {
    return;
  }
  const event = new CustomEvent("dashboard:event", {
    detail: {
      type,
      automationId,
      runId,
    },
  });
  window.dispatchEvent(event);
}
