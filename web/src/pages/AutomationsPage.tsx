import { useCallback, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactElement } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  createAutomation as createAutomationRecord,
  deleteAutomation as deleteAutomationRecord,
  fetchAutomationOverview,
  runAutomation as runAutomationTask,
  updateAutomation as updateAutomationRecord,
  fetchModels,
  type AutomationSummary,
  type AutomationOverviewSnapshot,
  type ModelConfig,
} from "../services/api";
import { useReadinessFlag } from "../lib/readiness-contract";
import { ConnectionStatus, useWebSocket, type WebSocketMessage } from "../lib/useWebSocket";
import { DEFAULT_TEXT_MODEL } from "../lib/model-config";
import { useAuth } from "../lib/auth";
import { PlusIcon } from "../components/icons";
import AutomationSettingsDrawer from "../components/automation-settings/AutomationSettingsDrawer";
import UsageWidget from "../components/UsageWidget";
import {
  Button,
  Table,
  SectionHeader,
  EmptyState,
  Spinner
} from "../components/ui";
import { useConfirm } from "../components/confirm";
import { AutomationTableRow } from "./automations/AutomationTableRow";
import { sortAutomations, loadSortConfig, persistSortConfig, type SortKey, type SortConfig, type AutomationRunsState } from "./automations/sorting";
import { applyRunUpdate, applyAutomationStateUpdate } from "./automations/websocketHandlers";
import {
  useAutomationOverviewRealtimeManager,
  useAutomationOverviewRealtimeSubscriptions,
} from "./automations/useAutomationOverviewRealtime";

// App logo (served from public folder)
const appLogo = "/Gemini_Generated_Image_klhmhfklhmhfklhm-removebg-preview.png";

type Scope = "my" | "all";

const RUNS_LIMIT = 50;

function parseAutomationTopic(topic: string): number | null {
  if (!topic.startsWith("automation:")) {
    return null;
  }

  const [, automationIdRaw] = topic.split(":");
  const automationId = Number.parseInt(automationIdRaw ?? "", 10);
  return Number.isFinite(automationId) ? automationId : null;
}

function isAutomationLifecycleEvent(eventType: string): boolean {
  return eventType === "automation_state" || eventType === "automation_updated";
}

export default function AutomationsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAuthenticated, user } = useAuth();
  const confirm = useConfirm();
  const [scope, setScope] = useState<Scope>("my");
  const [sortConfig, setSortConfig] = useState<SortConfig>(() => loadSortConfig());
  const [expandedAutomationId, setExpandedAutomationId] = useState<number | null>(null);
  const [expandedRunHistory, setExpandedRunHistory] = useState<Set<number>>(new Set());
  const [settingsAutomationId, setSettingsAutomationId] = useState<number | null>(null);
  const [editingAutomationId, setEditingAutomationId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState<string>("");
  const canViewAllAutomations = user?.role === "ADMIN";
  const effectiveScope: Scope = canViewAllAutomations ? scope : "my";
  const automationOverviewQueryKey = useMemo(
    () => ["automations-overview", effectiveScope, RUNS_LIMIT] as const,
    [effectiveScope],
  );
  const realtimeManager = useAutomationOverviewRealtimeManager();

  const applyAutomationOverviewUpdate = useCallback(
    (updater: (current: AutomationOverviewSnapshot) => AutomationOverviewSnapshot) => {
      queryClient.setQueryData<AutomationOverviewSnapshot>(automationOverviewQueryKey, (current) => {
        if (!current) {
          return current;
        }
        return updater(current);
      });
    },
    [automationOverviewQueryKey, queryClient]
  );

  const handleWebSocketMessage = useCallback(
    (message: WebSocketMessage) => {
      if (!message || typeof message !== "object") {
        return;
      }

      if (realtimeManager.handleControlMessage(message)) {
        return;
      }

      const topic = typeof message.topic === "string" ? message.topic : "";
      const automationId = parseAutomationTopic(topic);
      if (automationId == null) {
        return;
      }

      const dataPayload =
        typeof message.data === "object" && message.data !== null ? (message.data as Record<string, unknown>) : {};
      const eventType = message.type;

      if (isAutomationLifecycleEvent(eventType)) {
        applyAutomationOverviewUpdate((current) => applyAutomationStateUpdate(current, automationId, dataPayload));
        return;
      }

      if (eventType === "run_update") {
        applyAutomationOverviewUpdate((current) => applyRunUpdate(current, automationId, dataPayload));
        return;
      }
    },
    [applyAutomationOverviewUpdate, realtimeManager]
  );

  const { connectionStatus, sendMessage } = useWebSocket(isAuthenticated, {
    onMessage: handleWebSocketMessage,
    onConnect: realtimeManager.handleConnect,
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
    data: automationOverviewData,
    isLoading,
    error,
  } = useQuery<AutomationOverviewSnapshot>({
    queryKey: automationOverviewQueryKey,
    queryFn: () => fetchAutomationOverview({ scope: effectiveScope, runsLimit: RUNS_LIMIT }),
    refetchInterval: connectionStatus === ConnectionStatus.CONNECTED ? false : 2000,
  });

  const automations: AutomationSummary[] = useMemo(() => automationOverviewData?.automations ?? [], [automationOverviewData]);
  const automationIds = useMemo(() => automations.map((automation) => automation.id), [automations]);
  const visibleAutomationIds = useMemo(() => new Set(automationIds), [automationIds]);
  const activeExpandedAutomationId =
    expandedAutomationId !== null && visibleAutomationIds.has(expandedAutomationId) ? expandedAutomationId : null;
  const activeSettingsAutomationId =
    settingsAutomationId !== null && visibleAutomationIds.has(settingsAutomationId) ? settingsAutomationId : null;

  const runsByAutomation: AutomationRunsState = useMemo(() => {
    if (!automationOverviewData) {
      return {};
    }

    const lookup: AutomationRunsState = {};
    for (const bundle of automationOverviewData.runs) {
      lookup[bundle.automationId] = bundle.runs;
    }

    for (const automation of automationOverviewData.automations) {
      if (!lookup[automation.id]) {
        lookup[automation.id] = [];
      }
    }

    return lookup;
  }, [automationOverviewData]);

  const runsDataLoading = isLoading && !automationOverviewData;

  useAutomationOverviewRealtimeSubscriptions({
    automationIds,
    connectionStatus,
    enabled: isAuthenticated,
    manager: realtimeManager,
    sendMessage,
  });

  // Readiness Contract (see src/lib/readiness-contract.ts):
  // - data-ready="true": Page is INTERACTIVE (can click, type)
  // - data-screenshot-ready="true": Content loaded for marketing captures
  useReadinessFlag({
    ready: !isLoading,
    // The automations table is visible even if empty, so screenshot readiness
    // matches interactive readiness here.
    screenshotReady: !isLoading,
  });

  // Mutation for starting an automation run (hybrid: optimistic + WebSocket)
  const startRunMutation = useMutation({
    mutationFn: runAutomationTask,
    onMutate: async (automationId: number) => {
      await queryClient.cancelQueries({ queryKey: automationOverviewQueryKey });

      const previousSnapshot = queryClient.getQueryData<AutomationOverviewSnapshot>(automationOverviewQueryKey);

      queryClient.setQueryData<AutomationOverviewSnapshot>(automationOverviewQueryKey, (current) => {
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
        queryClient.setQueryData(automationOverviewQueryKey, context.previousSnapshot);
      }
      console.error("Failed to start run:", err);
    },
    onSettled: (_, __, automationId) => {
      dispatchAutomationEvent("run", automationId);
    },
  });

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
      queryClient.invalidateQueries({ queryKey: automationOverviewQueryKey });
      idempotencyKeyRef.current = null; // Reset for next creation
    },
  });

  // Delete automation mutation
  const deleteAutomationMutation = useMutation({
    mutationFn: deleteAutomationRecord,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: automationOverviewQueryKey });
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
      queryClient.invalidateQueries({ queryKey: automationOverviewQueryKey });
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
      <div id="automations-container" className="automations-page">
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
      <div id="automations-container" className="automations-page">
        <EmptyState
          variant="error"
          title="Error loading automations"
          description={message}
          action={
            <Button onClick={() => queryClient.invalidateQueries({ queryKey: automationOverviewQueryKey })}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }

  const includeOwner = effectiveScope === "all";
  const emptyColspan = includeOwner ? 8 : 7;

  return (
    <div id="automations-container" className="automations-page">
      <SectionHeader
        className="automations-hero"
        title={effectiveScope === "all" ? "All automations" : "My automations"}
        description="Monitor and manage your automations."
        actions={
          <div className="automations-actions">
            <div className="automations-actions__stats">
              <UsageWidget />
            </div>
            <div className="automations-actions__controls">
              {canViewAllAutomations && (
                <div className="scope-wrapper">
                  <label className="scope-toggle">
                    <input
                      type="checkbox"
                      id="automations-scope-toggle"
                      data-testid="automations-scope-toggle"
                      aria-label="Toggle between my automations and all automations"
                      checked={effectiveScope === "all"}
                      onChange={(e) => {
                        const newScope = e.target.checked ? "all" : "my";
                        setScope(newScope);
                      }}
                    />
                    <span className="slider"></span>
                  </label>
                </div>
              )}
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

      <div className="automations-content">
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
                isExpanded={activeExpandedAutomationId === automation.id}
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
                onRunActionsClick={dispatchAutomationEvent.bind(null, "run-actions")}
              />
            ))}
            {sortedAutomations.length === 0 && (
              <Table.Row className="automations-empty-row">
                <Table.Cell colSpan={emptyColspan} className="automations-empty-cell">
                  <EmptyState
                    icon={<img src={appLogo} alt="Longhouse Logo" className="automations-empty-logo" />}
                    title="No automations found"
                    description="Click 'Create Automation' to get started."
                  />
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table>
      </div>
      {activeSettingsAutomationId != null && (
        <AutomationSettingsDrawer
          automationId={activeSettingsAutomationId}
          isOpen={activeSettingsAutomationId != null}
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
      const next =
        prev.key === key ? { key, ascending: !prev.ascending } : { key, ascending: true };
      persistSortConfig(next);
      return next;
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
    navigate(`/automations/${automationId}/thread/?name=${encodeURIComponent(automationName)}`);
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

type AutomationEventType = "run" | "edit" | "debug" | "delete" | "run-actions";

function dispatchAutomationEvent(type: AutomationEventType, automationId: number, runId?: number) {
  if (typeof window === "undefined") {
    return;
  }
  const event = new CustomEvent("automations:event", {
    detail: {
      type,
      automationId,
      runId,
    },
  });
  window.dispatchEvent(event);
}
