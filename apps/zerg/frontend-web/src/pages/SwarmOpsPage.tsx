import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge, Button, Card, EmptyState, PageShell, SectionHeader, Spinner } from "../components/ui";
import { request } from "../services/api";
import "../styles/swarm-ops.css";

type AttentionLevel = "auto" | "soft" | "needs" | "hard";
type RunFilter = "all" | "attention" | "active" | "done";

type JarvisRunSummary = {
  id: number;
  agent_id: number;
  thread_id?: number;
  agent_name: string;
  status: string;
  summary?: string | null;
  signal?: string | null;
  signal_source?: string | null;
  error?: string | null;
  last_event_type?: string | null;
  last_event_message?: string | null;
  last_event_at?: string | null;
  continuation_of_run_id?: number | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
};

type RunWithAttention = JarvisRunSummary & { attention: AttentionLevel };

type RunEvent = {
  id: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type RunEventsResponse = {
  run_id: number;
  events: RunEvent[];
  total: number;
};

const RUN_LIMIT = 120;
const ACTIVE_STATUSES = new Set(["queued", "running", "waiting", "deferred"]);

const LEVEL_ORDER: Record<AttentionLevel, number> = {
  hard: 0,
  needs: 1,
  soft: 2,
  auto: 3,
};

const LEVEL_LABEL: Record<AttentionLevel, string> = {
  auto: "Auto",
  soft: "Nudge",
  needs: "Needs You",
  hard: "Hard Stop",
};

const SIGNAL_SOURCE_LABEL: Record<string, string> = {
  summary: "Summary",
  error: "Error",
  last_message: "Last message",
  last_event: "Last event",
};

const STATUS_BADGE_VARIANT: Record<string, "neutral" | "success" | "warning" | "error"> = {
  queued: "neutral",
  running: "warning",
  waiting: "warning",
  deferred: "warning",
  success: "success",
  failed: "error",
  cancelled: "error",
};

function formatRelativeTime(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSeconds = Math.floor(diffMs / 1000);
  if (diffSeconds < 5) return "just now";
  if (diffSeconds < 60) return `${diffSeconds}s ago`;
  const diffMinutes = Math.floor(diffSeconds / 60);
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays}d ago`;
}

function classifyAttention(run: JarvisRunSummary): AttentionLevel {
  const status = (run.status || "").toLowerCase();
  const signalText = (run.signal || run.summary || "").toLowerCase();

  if (status === "failed" || status === "cancelled") {
    return "hard";
  }

  if (run.error) {
    return "hard";
  }

  const hardSignals = /(broke|broken|rollback|incident|outage|data loss|corrupt|security|permission|prod|production|panic|crash|exception)/i;
  if (hardSignals.test(signalText)) {
    return "hard";
  }

  const needsSignals = /(need your input|your call|decide|choice|pick one|which one|approve|sign off|confirm path)/i;
  if (needsSignals.test(signalText)) {
    return "needs";
  }

  const softSignals = /(should i|do you want|want me to|ok to|proceed|continue|next step|shall i)\b/i;
  if (softSignals.test(signalText) || signalText.trim().endsWith("?")) {
    return "soft";
  }

  if (ACTIVE_STATUSES.has(status)) {
    return "auto";
  }

  return "auto";
}

function getEventSummary(event: RunEvent): string {
  const payload = event.payload || {};
  const message = typeof payload.message === "string" ? payload.message : null;
  const summary = typeof payload.summary === "string" ? payload.summary : null;
  const error = typeof payload.error === "string" ? payload.error : null;
  const toolName = typeof payload.tool_name === "string" ? payload.tool_name : null;
  const status = typeof payload.status === "string" ? payload.status : null;

  if (message) return message;
  if (summary) return summary;
  if (error) return error;
  if (toolName) return `tool: ${toolName}`;
  if (status) return `status: ${status}`;
  return "";
}

export default function SwarmOpsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [filter, setFilter] = useState<RunFilter>("attention");
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);

  const runsQuery = useQuery({
    queryKey: ["swarm-ops", "runs", RUN_LIMIT],
    queryFn: () => request<JarvisRunSummary[]>(`/jarvis/runs?limit=${RUN_LIMIT}`),
    refetchInterval: 5000,
  });

  const demoScenario = useMemo(() => {
    const params = new URLSearchParams(location.search);
    const value = params.get("demo");
    return value && value.trim().length > 0 ? value.trim() : null;
  }, [location.search]);

  useEffect(() => {
    if (!demoScenario) return;

    const storageKey = `swarm-demo-seeded:${demoScenario}`;
    if (typeof window !== "undefined" && window.sessionStorage.getItem(storageKey)) {
      return;
    }

    let cancelled = false;

    const seedScenario = async () => {
      try {
        await request(`/admin/seed-scenario`, {
          method: "POST",
          body: JSON.stringify({ name: demoScenario, clean: true }),
        });
        if (typeof window !== "undefined") {
          window.sessionStorage.setItem(storageKey, "1");
        }
        if (!cancelled) {
          runsQuery.refetch();
        }
      } catch (error) {
        // Ignore demo seeding failures (prod blocks this endpoint).
        // eslint-disable-next-line no-console
        console.warn("Swarm demo seeding failed:", error);
      }
    };

    void seedScenario();

    return () => {
      cancelled = true;
    };
  }, [demoScenario, runsQuery]);

  const runs = useMemo<RunWithAttention[]>(() => {
    return (runsQuery.data ?? []).map((run) => ({
      ...run,
      attention: classifyAttention(run),
    }));
  }, [runsQuery.data]);

  const sortedRuns = useMemo(() => {
    return [...runs].sort((a, b) => {
      const levelDiff = LEVEL_ORDER[a.attention] - LEVEL_ORDER[b.attention];
      if (levelDiff !== 0) return levelDiff;
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
  }, [runs]);

  const visibleRuns = useMemo(() => {
    switch (filter) {
      case "attention":
        return sortedRuns.filter((run) => run.attention === "needs" || run.attention === "hard");
      case "active":
        return sortedRuns.filter((run) => ACTIVE_STATUSES.has(run.status));
      case "done":
        return sortedRuns.filter((run) => !ACTIVE_STATUSES.has(run.status));
      default:
        return sortedRuns;
    }
  }, [filter, sortedRuns]);

  useEffect(() => {
    if (sortedRuns.length === 0) {
      setSelectedRunId(null);
      return;
    }

    if (selectedRunId === null || !sortedRuns.some((run) => run.id === selectedRunId)) {
      setSelectedRunId(sortedRuns[0].id);
    }
  }, [sortedRuns, selectedRunId]);

  const selectedRun = sortedRuns.find((run) => run.id === selectedRunId) ?? null;
  const shouldPollEvents = selectedRun ? ACTIVE_STATUSES.has(selectedRun.status) : false;

  const runEventsQuery = useQuery({
    queryKey: ["swarm-ops", "events", selectedRunId],
    enabled: selectedRunId != null,
    queryFn: () => request<RunEventsResponse>(`/jarvis/runs/${selectedRunId}/events?limit=120`),
    refetchInterval: shouldPollEvents ? 5000 : false,
  });

  const attentionCounts = useMemo(() => {
    const counts = {
      hard: 0,
      needs: 0,
      soft: 0,
      auto: 0,
      active: 0,
      total: runs.length,
    };

    for (const run of runs) {
      counts[run.attention] += 1;
      if (ACTIVE_STATUSES.has(run.status)) {
        counts.active += 1;
      }
    }

    return counts;
  }, [runs]);

  if (runsQuery.isLoading) {
    return (
      <div className="swarm-ops-loading">
        <Spinner size="lg" />
        <span>Loading swarm activity...</span>
      </div>
    );
  }

  if (runsQuery.error) {
    return (
      <div className="swarm-ops-loading">
        <EmptyState
          variant="error"
          title="Failed to load runs"
          description={runsQuery.error instanceof Error ? runsQuery.error.message : "Unknown error"}
        />
      </div>
    );
  }

  return (
    <PageShell size="wide" className="swarm-ops-shell">
      <div className="swarm-ops-page">
        <SectionHeader
          title="Swarm Ops"
          description="Triage active runs, jump to context, and keep the swarm flowing."
          actions={
            <div className="swarm-ops-actions">
              <Button
                variant={filter === "attention" ? "primary" : "secondary"}
                size="sm"
                onClick={() => setFilter("attention")}
              >
                Needs attention
              </Button>
              <Button
                variant={filter === "active" ? "primary" : "secondary"}
                size="sm"
                onClick={() => setFilter("active")}
              >
                Active
              </Button>
              <Button
                variant={filter === "done" ? "primary" : "secondary"}
                size="sm"
                onClick={() => setFilter("done")}
              >
                Completed
              </Button>
              <Button
                variant={filter === "all" ? "primary" : "secondary"}
                size="sm"
                onClick={() => setFilter("all")}
              >
                All
              </Button>
            </div>
          }
        />

        <div className="swarm-ops-summary">
          <Card className="swarm-ops-summary-card">
            <div className="swarm-ops-summary-label">Hard stops</div>
            <div className="swarm-ops-summary-value">{attentionCounts.hard}</div>
          </Card>
          <Card className="swarm-ops-summary-card">
            <div className="swarm-ops-summary-label">Needs you</div>
            <div className="swarm-ops-summary-value">{attentionCounts.needs}</div>
          </Card>
          <Card className="swarm-ops-summary-card">
            <div className="swarm-ops-summary-label">Active</div>
            <div className="swarm-ops-summary-value">{attentionCounts.active}</div>
          </Card>
          <Card className="swarm-ops-summary-card">
            <div className="swarm-ops-summary-label">Total runs</div>
            <div className="swarm-ops-summary-value">{attentionCounts.total}</div>
          </Card>
        </div>

        {runs.length === 0 ? (
          <EmptyState
            title="No runs yet"
            description="Kick off a task in Jarvis and it will show up here for triage."
          />
        ) : (
          <div className="swarm-ops-layout">
            <div className="swarm-ops-list">
              <div className="swarm-ops-list-header">
                <div>
                  <div className="swarm-ops-list-title">Run queue</div>
                  <div className="swarm-ops-list-subtitle">Sorted by urgency, newest first</div>
                </div>
                <div className="swarm-ops-list-count">{visibleRuns.length} shown</div>
              </div>

              <div className="swarm-ops-list-body">
                {visibleRuns.map((run) => {
                  const statusVariant = STATUS_BADGE_VARIANT[run.status] ?? "neutral";
                  const isSelected = run.id === selectedRunId;
                  const signalText = run.signal || run.summary || "No signal yet";
                  const signalSourceLabel = run.signal_source ? SIGNAL_SOURCE_LABEL[run.signal_source] ?? "Signal" : null;
                  const lastEventLine = run.last_event_type
                    ? `Last: ${run.last_event_type}${run.last_event_at ? ` · ${formatRelativeTime(run.last_event_at)}` : ""}`
                    : null;

                  return (
                    <button
                      key={run.id}
                      type="button"
                      className={clsx("swarm-ops-item", `swarm-ops-item--${run.attention}`, {
                        "swarm-ops-item--active": isSelected,
                      })}
                      onClick={() => setSelectedRunId(run.id)}
                      aria-pressed={isSelected}
                    >
                      <div className="swarm-ops-item-main">
                        <div className="swarm-ops-item-title-row">
                          <span className="swarm-ops-item-title">{run.agent_name}</span>
                          <Badge variant={statusVariant}>{run.status}</Badge>
                        </div>
                        <div className="swarm-ops-item-summary">
                          {signalText}
                        </div>
                        {(signalSourceLabel || lastEventLine) && (
                          <div className="swarm-ops-item-signal">
                            {signalSourceLabel ? `Signal: ${signalSourceLabel}` : "Signal"}
                            {lastEventLine ? ` · ${lastEventLine}` : ""}
                          </div>
                        )}
                      </div>
                      <div className="swarm-ops-item-meta">
                        <span className={clsx("swarm-ops-attention-pill", `swarm-ops-attention-pill--${run.attention}`)}>
                          {LEVEL_LABEL[run.attention]}
                        </span>
                        <span className="swarm-ops-item-time">{formatRelativeTime(run.created_at)}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="swarm-ops-detail">
              {selectedRun ? (
                <Card className="swarm-ops-detail-card">
                  <Card.Header className="swarm-ops-detail-header">
                    <div>
                      <div className="swarm-ops-detail-title">{selectedRun.agent_name}</div>
                      <div className="swarm-ops-detail-subtitle">
                        Run #{selectedRun.id} · {selectedRun.status} · {formatRelativeTime(selectedRun.created_at)}
                      </div>
                    </div>
                    <span className={clsx("swarm-ops-attention-pill", `swarm-ops-attention-pill--${selectedRun.attention}`)}>
                      {LEVEL_LABEL[selectedRun.attention]}
                    </span>
                  </Card.Header>

                  <Card.Body>
                    <div className="swarm-ops-detail-section">
                      <div className="swarm-ops-detail-label">Signal</div>
                      <p className="swarm-ops-detail-summary">
                        {selectedRun.signal || selectedRun.summary || "No signal recorded yet."}
                      </p>
                      {selectedRun.signal_source && (
                        <div className="swarm-ops-detail-meta">
                          Source: {SIGNAL_SOURCE_LABEL[selectedRun.signal_source] ?? selectedRun.signal_source}
                        </div>
                      )}
                    </div>

                    {selectedRun.error && (
                      <div className="swarm-ops-detail-section">
                        <div className="swarm-ops-detail-label">Error</div>
                        <p className="swarm-ops-detail-error">{selectedRun.error}</p>
                      </div>
                    )}

                    {(selectedRun.last_event_type || selectedRun.last_event_message) && (
                      <div className="swarm-ops-detail-section">
                        <div className="swarm-ops-detail-label">Last event</div>
                        <div className="swarm-ops-detail-meta">
                          {selectedRun.last_event_type ?? "event"}
                          {selectedRun.last_event_at ? ` · ${formatRelativeTime(selectedRun.last_event_at)}` : ""}
                        </div>
                        {selectedRun.last_event_message && (
                          <p className="swarm-ops-detail-summary">{selectedRun.last_event_message}</p>
                        )}
                      </div>
                    )}

                    <div className="swarm-ops-detail-actions">
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={!selectedRun.thread_id}
                        onClick={() => {
                          if (selectedRun.thread_id) {
                            navigate(`/agent/${selectedRun.agent_id}/thread/${selectedRun.thread_id}`);
                          }
                        }}
                      >
                        Open thread
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => runsQuery.refetch()}
                      >
                        Refresh
                      </Button>
                    </div>

                    <div className="swarm-ops-detail-section">
                      <div className="swarm-ops-detail-label">Recent events</div>
                      {runEventsQuery.isLoading ? (
                        <div className="swarm-ops-events-loading">
                          <Spinner size="sm" />
                          <span>Loading events...</span>
                        </div>
                      ) : runEventsQuery.data?.events?.length ? (
                        <div className="swarm-ops-events">
                          {runEventsQuery.data.events.map((event) => {
                            const summary = getEventSummary(event);
                            return (
                              <div key={event.id} className="swarm-ops-event">
                                <div className="swarm-ops-event-main">
                                  <div className="swarm-ops-event-type">{event.event_type}</div>
                                  {summary && <div className="swarm-ops-event-summary">{summary}</div>}
                                </div>
                                <div className="swarm-ops-event-meta">{formatRelativeTime(event.created_at)}</div>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="swarm-ops-events-empty">No events recorded yet.</div>
                      )}
                    </div>
                  </Card.Body>
                </Card>
              ) : (
                <Card className="swarm-ops-detail-card swarm-ops-detail-empty">
                  <Card.Body>
                    <div className="swarm-ops-detail-title">No run selected</div>
                    <p className="swarm-ops-detail-summary">Pick a run from the left to inspect details.</p>
                  </Card.Body>
                </Card>
              )}
            </div>
          </div>
        )}
      </div>
    </PageShell>
  );
}
