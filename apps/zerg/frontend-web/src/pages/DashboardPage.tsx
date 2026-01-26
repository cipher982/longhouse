import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactElement } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  fetchDashboardSnapshot,
  runFiche,
  updateFiche,
  fetchModels,
  type Course,
  type FicheSummary,
  type DashboardSnapshot,
  type ModelConfig,
} from "../services/api";
import { buildUrl } from "../services/api";
import { ConnectionStatus, useWebSocket } from "../lib/useWebSocket";
import { useAuth } from "../lib/auth";
import { PlusIcon } from "../components/icons";
import FicheSettingsDrawer from "../components/fiche-settings/FicheSettingsDrawer";
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
import { FicheTableRow } from "./dashboard/FicheTableRow";
import { sortFiches, loadSortConfig, persistSortConfig, type SortKey, type SortConfig, type FicheCoursesState } from "./dashboard/sorting";
import { applyCourseUpdate, applyFicheStateUpdate } from "./dashboard/websocketHandlers";

// App logo (served from public folder)
const appLogo = "/Gemini_Generated_Image_klhmhfklhmhfklhm-removebg-preview.png";

type Scope = "my" | "all";

const COURSES_LIMIT = 50;

export default function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();
  const confirm = useConfirm();
  const [scope, setScope] = useState<Scope>("my");
  const [sortConfig, setSortConfig] = useState<SortConfig>(() => loadSortConfig());
  const [expandedFicheId, setExpandedFicheId] = useState<number | null>(null);
  const dashboardQueryKey = useMemo(() => ["dashboard", scope, COURSES_LIMIT] as const, [scope]);
  const [expandedCourseHistory, setExpandedCourseHistory] = useState<Set<number>>(new Set());
  const [settingsFicheId, setSettingsFicheId] = useState<number | null>(null);
  const [editingFicheId, setEditingFicheId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState<string>("");

  // WebSocket state - must be declared before useQuery to avoid reference errors
  const subscribedFicheIdsRef = useRef<Set<number>>(new Set());
  const [wsReconnectToken, setWsReconnectToken] = useState(0);
  const sendMessageRef = useRef<((message: any) => void) | null>(null);
  const messageIdCounterRef = useRef(0);

  // Track pending subscriptions to handle confirmations and timeouts
  // Don't mark as subscribed until we get subscribe_ack to enable automatic retry
  const pendingSubscriptionsRef = useRef<Map<string, { topics: string[]; timeoutId: number; ficheIds: number[] }>>(new Map());

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
              pending.ficheIds.forEach((ficheId) => {
                subscribedFicheIdsRef.current.add(ficheId);
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
      if (!topic.startsWith("fiche:")) {
        return;
      }

      const [, ficheIdRaw] = topic.split(":");
      const ficheId = Number.parseInt(ficheIdRaw ?? "", 10);
      if (!Number.isFinite(ficheId)) {
        return;
      }

      const dataPayload =
        typeof message.data === "object" && message.data !== null ? (message.data as Record<string, unknown>) : {};
      const eventType = message.type;

      if (eventType === "fiche_state" || eventType === "fiche_updated") {
        applyDashboardUpdate((current) => applyFicheStateUpdate(current, ficheId, dataPayload));
        return;
      }

      if (eventType === "course_update") {
        applyDashboardUpdate((current) => applyCourseUpdate(current, ficheId, dataPayload));
        return;
      }
    },
    [applyDashboardUpdate]
  );

  const { connectionStatus, sendMessage } = useWebSocket(isAuthenticated, {
    onMessage: handleWebSocketMessage,
    onConnect: () => {
      subscribedFicheIdsRef.current.clear();
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
    queryFn: () => fetchDashboardSnapshot({ scope, coursesLimit: COURSES_LIMIT }),
    refetchInterval: connectionStatus === ConnectionStatus.CONNECTED ? false : 2000,
  });

  const fiches: FicheSummary[] = useMemo(() => dashboardData?.fiches ?? [], [dashboardData]);

  const coursesByFiche: FicheCoursesState = useMemo(() => {
    if (!dashboardData) {
      return {};
    }

    const lookup: FicheCoursesState = {};
    for (const bundle of dashboardData.courses) {
      lookup[bundle.ficheId] = bundle.courses;
    }

    for (const fiche of dashboardData.fiches) {
      if (!lookup[fiche.id]) {
        lookup[fiche.id] = [];
      }
    }

    return lookup;
  }, [dashboardData]);

  const coursesDataLoading = isLoading && !dashboardData;

  // Readiness Contract (see src/lib/readiness-contract.ts):
  // - data-ready="true": Page is INTERACTIVE (can click, type)
  // - data-screenshot-ready="true": Content loaded for marketing captures
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute('data-ready', 'true');
      // Dashboard is screenshot-ready as soon as it's interactive
      // (fiches table is visible even if empty)
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

  // Mutation for starting a fiche course (hybrid: optimistic + WebSocket)
  const startCourseMutation = useMutation({
    mutationFn: runFiche,
    onMutate: async (ficheId: number) => {
      await queryClient.cancelQueries({ queryKey: dashboardQueryKey });

      const previousSnapshot = queryClient.getQueryData<DashboardSnapshot>(dashboardQueryKey);

      queryClient.setQueryData<DashboardSnapshot>(dashboardQueryKey, (current) => {
        if (!current) {
          return current;
        }

        return {
          ...current,
          fiches: current.fiches.map((fiche) =>
            fiche.id === ficheId ? { ...fiche, status: "running" as const } : fiche
          ),
        };
      });

      return { previousSnapshot };
    },
    onError: (err: Error, ficheId: number, context) => {
      if (context?.previousSnapshot) {
        queryClient.setQueryData(dashboardQueryKey, context.previousSnapshot);
      }
      console.error("Failed to start course:", err);
    },
    onSettled: (_, __, ficheId) => {
      dispatchDashboardEvent("course", ficheId);
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
    if (expandedFicheId === null) {
      return;
    }
    if (fiches.some((fiche) => fiche.id === expandedFicheId)) {
      return;
    }
    setExpandedFicheId(null);
  }, [fiches, expandedFicheId]);

  // Use unified WebSocket hook for real-time updates
  // Only connect when authenticated to avoid auth failure spam
  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    if (connectionStatus !== ConnectionStatus.CONNECTED) {
      return;
    }

    const activeIds = new Set(fiches.map((fiche) => fiche.id));

    // Find fiches that need subscription (not currently subscribed AND not pending)
    const pendingFicheIds = new Set<number>();
    pendingSubscriptionsRef.current.forEach((pending) => {
      pending.ficheIds.forEach((id) => pendingFicheIds.add(id));
    });

    const topicsToSubscribe: string[] = [];
    const ficheIdsToSubscribe: number[] = [];
    for (const id of activeIds) {
      if (!subscribedFicheIdsRef.current.has(id) && !pendingFicheIds.has(id)) {
        topicsToSubscribe.push(`fiche:${id}`);
        ficheIdsToSubscribe.push(id);
      }
    }

    const topicsToUnsubscribe: string[] = [];
    for (const id of Array.from(subscribedFicheIdsRef.current)) {
      if (!activeIds.has(id)) {
        subscribedFicheIdsRef.current.delete(id);
        topicsToUnsubscribe.push(`fiche:${id}`);
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
        ficheIds: ficheIdsToSubscribe
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
  }, [fiches, connectionStatus, isAuthenticated, wsReconnectToken, generateMessageId]);

  useEffect(() => {
    if (isAuthenticated) {
      return;
    }

    if (subscribedFicheIdsRef.current.size === 0) {
      return;
    }

    const topics = Array.from(subscribedFicheIdsRef.current).map((id) => `fiche:${id}`);
    sendMessageRef.current?.({
      type: "unsubscribe",
      topics,
      message_id: generateMessageId(),
    });
    subscribedFicheIdsRef.current.clear();
  }, [isAuthenticated, generateMessageId]);

  // Cleanup effect - runs only on unmount to unsubscribe from all fiches
  /* eslint-disable react-hooks/exhaustive-deps -- Intentional: cleanup reads current values at unmount time */
  useEffect(() => {
    // Capture refs for cleanup (ESLint wants this pattern)
    const pendingSubscriptions = pendingSubscriptionsRef.current;
    const subscribedFicheIds = subscribedFicheIdsRef.current;
    const sendMessage = sendMessageRef.current;
    const msgId = generateMessageId; // Capture for cleanup

    return () => {
      // Clear pending subscription timeouts
      pendingSubscriptions.forEach((pending) => {
        clearTimeout(pending.timeoutId);
      });
      pendingSubscriptions.clear();

      if (subscribedFicheIds.size === 0) {
        return;
      }
      const topics = Array.from(subscribedFicheIds).map((id) => `fiche:${id}`);
      sendMessage?.({
        type: "unsubscribe",
        topics,
        message_id: msgId(),
      });
      subscribedFicheIds.clear();
    };
  }, []);
  /* eslint-enable react-hooks/exhaustive-deps */

  // Generate idempotency key per mutation to prevent double-creates
  const idempotencyKeyRef = useRef<string | null>(null);

  const createFicheMutation = useMutation({
    mutationFn: async () => {
      // Generate fresh key for each create attempt
      const key = `create-fiche-${Date.now()}-${Math.random()}`;
      idempotencyKeyRef.current = key;

      // Backend auto-generates name as "Fiche #<id>"
      const response = await fetch(buildUrl("/fiches"), {
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
        throw new Error(`Failed to create fiche: ${response.status}`);
      }

      return response.json();
    },
    onSuccess: () => {
      // WebSocket will deliver the fiche with real name
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
      idempotencyKeyRef.current = null; // Reset for next creation
    },
  });

  // Delete fiche mutation
  const deleteFicheMutation = useMutation({
    mutationFn: async (ficheId: number) => {
      const response = await fetch(buildUrl(`/fiches/${ficheId}`), {
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
  function startEditingName(ficheId: number, currentName: string) {
    setEditingFicheId(ficheId);
    setEditingName(currentName);
  }

  async function saveNameAndExit(ficheId: number) {
    if (!editingName.trim()) {
      // Don't allow empty names
      return;
    }

    try {
      await updateFiche(ficheId, { name: editingName });
      queryClient.invalidateQueries({ queryKey: dashboardQueryKey });
    } catch (error) {
      console.error("Failed to rename:", error);
    }

    setEditingFicheId(null);
    setEditingName("");
  }

  function cancelEditing() {
    setEditingFicheId(null);
    setEditingName("");
  }

  const sortedFiches = useMemo(() => {
    return sortFiches(fiches, coursesByFiche, sortConfig);
  }, [fiches, coursesByFiche, sortConfig]);

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
    const message = error instanceof Error ? error.message : "Failed to load fiches";
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
              onClick={() => createFicheMutation.mutate()}
              disabled={createFicheMutation.isPending}
              data-testid="create-fiche-btn"
            >
              {createFicheMutation.isPending ? (
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

        <Table className="fiches-table">
          <Table.Header>
            {renderHeaderCell("Name", "name", sortConfig, handleSort)}
            {includeOwner && renderHeaderCell("Owner", "owner", sortConfig, handleSort, false)}
            {renderHeaderCell("Status", "status", sortConfig, handleSort)}
            {renderHeaderCell("Created", "created_at", sortConfig, handleSort)}
            {renderHeaderCell("Last Course", "last_course", sortConfig, handleSort)}
            {renderHeaderCell("Next Course", "next_course", sortConfig, handleSort)}
            {renderHeaderCell("Success Rate", "success", sortConfig, handleSort)}
            <Table.Cell isHeader className="actions-header">
              Actions
            </Table.Cell>
          </Table.Header>
          <Table.Body id="fiches-table-body">
            {sortedFiches.map((fiche) => (
              <FicheTableRow
                key={fiche.id}
                fiche={fiche}
                courses={coursesByFiche[fiche.id] || []}
                includeOwner={includeOwner}
                isExpanded={expandedFicheId === fiche.id}
                isCourseHistoryExpanded={expandedCourseHistory.has(fiche.id)}
                isPendingCourse={startCourseMutation.isPending && startCourseMutation.variables === fiche.id}
                coursesDataLoading={coursesDataLoading}
                editingFicheId={editingFicheId}
                editingName={editingName}
                onToggleRow={toggleFicheRow}
                onToggleCourseHistory={toggleCourseHistory}
                onRunFiche={handleStartCourse}
                onChatFiche={handleChatFiche}
                onDebugFiche={handleDebugFiche}
                onDeleteFiche={handleDeleteFiche}
                onStartEditingName={startEditingName}
                onSaveNameAndExit={saveNameAndExit}
                onCancelEditing={cancelEditing}
                onEditingNameChange={setEditingName}
                onCourseActionsClick={dispatchDashboardEvent.bind(null, "course-actions")}
              />
            ))}
            {sortedFiches.length === 0 && (
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
      {settingsFicheId != null && (
        <FicheSettingsDrawer
          ficheId={settingsFicheId}
          isOpen={settingsFicheId != null}
          onClose={() => setSettingsFicheId(null)}
        />
      )}
    </div>
  );

  function toggleFicheRow(ficheId: number) {
    setExpandedFicheId((prev) => (prev === ficheId ? null : ficheId));
  }

  function toggleCourseHistory(ficheId: number) {
    setExpandedCourseHistory((prev) => {
      const next = new Set(prev);
      if (next.has(ficheId)) {
        next.delete(ficheId);
      } else {
        next.clear();
        next.add(ficheId);
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

  function handleStartCourse(event: ReactMouseEvent<HTMLButtonElement>, ficheId: number, status: string) {
    event.stopPropagation();
    // Don't run if already running
    if (status === "running") {
      return;
    }
    // Use the optimistic mutation
    startCourseMutation.mutate(ficheId);
  }

  function handleChatFiche(event: ReactMouseEvent<HTMLButtonElement>, ficheId: number, ficheName: string) {
    event.stopPropagation();
    navigate(`/fiche/${ficheId}/thread/?name=${encodeURIComponent(ficheName)}`);
  }

  function handleDebugFiche(event: ReactMouseEvent<HTMLButtonElement>, ficheId: number) {
    event.stopPropagation();
    setSettingsFicheId(ficheId);
  }

  async function handleDeleteFiche(event: ReactMouseEvent<HTMLButtonElement>, ficheId: number, name: string) {
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
    deleteFicheMutation.mutate(ficheId);
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

type DashboardEventType = "course" | "edit" | "debug" | "delete" | "course-actions";

function dispatchDashboardEvent(type: DashboardEventType, ficheId: number, courseId?: number) {
  if (typeof window === "undefined") {
    return;
  }
  const event = new CustomEvent("dashboard:event", {
    detail: {
      type,
      ficheId,
      courseId,
    },
  });
  window.dispatchEvent(event);
}
