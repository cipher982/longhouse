import React, { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../lib/auth";
import config from "../lib/config";
import {
  Button,
  Card,
  SectionHeader,
  EmptyState,
  Table,
  Badge
} from "../components/ui";

// Types for trace data
interface TraceListItem {
  trace_id: string;
  run_id: number;
  status: string | null;
  model: string | null;
  started_at: string | null;
  duration_ms: number | null;
}

interface TracesResponse {
  traces: TraceListItem[];
  limit: number;
  offset: number;
  count: number;
}

interface TimelineEvent {
  timestamp: string;
  event_type: string;
  source: "run" | "worker" | "llm";
  details: Record<string, unknown>;
  is_error: boolean;
  duration_ms: number | null;
}

interface TraceDetail {
  trace_id: string;
  status: string;
  started_at: string | null;
  duration_seconds: number;
  counts: {
    runs: number;
    workers: number;
    llm_calls: number;
  };
  anomalies: string[];
  timeline: TimelineEvent[];
  llm_details?: Array<{
    phase: string;
    model: string;
    message_count: number;
    input_tokens: number;
    output_tokens: number;
    duration_ms: number;
    error: string | null;
    response_preview?: string;
    tool_calls?: Array<{ name: string; args: unknown }>;
  }>;
}

// API functions
async function fetchTraces(limit: number = 20, offset: number = 0): Promise<TracesResponse> {
  const response = await fetch(
    `${config.apiBaseUrl}/traces?limit=${limit}&offset=${offset}`,
    { credentials: "include" }
  );

  if (!response.ok) {
    if (response.status === 403) {
      throw new Error("Admin access required");
    }
    throw new Error("Failed to fetch traces");
  }

  return response.json();
}

async function fetchTraceDetail(traceId: string, level: string = "summary"): Promise<TraceDetail> {
  const response = await fetch(
    `${config.apiBaseUrl}/traces/${traceId}?level=${level}`,
    { credentials: "include" }
  );

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Trace not found");
    }
    throw new Error("Failed to fetch trace detail");
  }

  return response.json();
}

// Source color scheme
const sourceStyles: Record<string, { color: string; bg: string; label: string }> = {
  run: { color: "#3b82f6", bg: "rgba(59, 130, 246, 0.15)", label: "RUN" },
  worker: { color: "#10b981", bg: "rgba(16, 185, 129, 0.15)", label: "WORKER" },
  llm: { color: "#a855f7", bg: "rgba(168, 85, 247, 0.15)", label: "LLM" },
};

// Timeline event component
function TimelineEventRow({ event, isLast }: { event: TimelineEvent; isLast: boolean }) {
  const style = sourceStyles[event.source] || { color: "#6b7280", bg: "rgba(107, 114, 128, 0.15)", label: event.source };

  const formatTime = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      fractionalSecondDigits: 3,
    });
  };

  const formatDuration = (ms: number) => {
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  };

  // Format details as readable pills
  const detailPills = Object.entries(event.details)
    .filter(([_, v]) => v != null && v !== "")
    .slice(0, 6) // Limit to 6 items
    .map(([k, v]) => ({ key: k, value: String(v).substring(0, 40) }));

  return (
    <div
      className={`trace-timeline-row${event.is_error ? " is-error" : ""}`}
      style={{ "--trace-color": style.color, "--trace-bg": style.bg } as React.CSSProperties}
    >
      {/* Timestamp column */}
      <div className="trace-time-col">{formatTime(event.timestamp)}</div>

      {/* Timeline indicator */}
      <div className="trace-indicator">
        <div className="trace-dot" />
        {!isLast && <div className="trace-line" />}
      </div>

      {/* Content */}
      <div className="trace-content">
        <div className="trace-header">
          {/* Event name */}
          <span className="trace-event-type">{event.event_type}</span>

          {/* Source badge */}
          <span className="trace-source-badge">{style.label}</span>

          {/* Duration */}
          {event.duration_ms != null && event.duration_ms > 0 && (
            <span className="trace-duration">{formatDuration(event.duration_ms)}</span>
          )}

          {/* Error badge */}
          {event.is_error && <span className="trace-error-badge">ERROR</span>}
        </div>

        {/* Detail pills */}
        {detailPills.length > 0 && (
          <div className="trace-detail-pills">
            {detailPills.map(({ key, value }) => (
              <span key={key} className="trace-detail-pill">
                <span className="trace-detail-pill-key">{key}</span>
                <span className="trace-detail-pill-sep">=</span>
                <span className="trace-detail-pill-value">{value}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Trace detail component
function TraceDetailView({
  traceId,
  onClose,
}: {
  traceId: string;
  onClose: () => void;
}) {
  const [level, setLevel] = useState<"summary" | "full" | "errors">("summary");

  const { data: detail, isLoading, error } = useQuery({
    queryKey: ["trace-detail", traceId, level],
    queryFn: () => fetchTraceDetail(traceId, level),
    enabled: !!traceId,
  });

  if (isLoading) {
    return (
      <Card>
        <Card.Body>
          <div className="trace-explorer-loading">Loading trace...</div>
        </Card.Body>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <Card.Body>
          <EmptyState variant="error" title="Error" description={String(error)} />
        </Card.Body>
      </Card>
    );
  }

  if (!detail) return null;

  const statusColors: Record<string, { color: string; bg: string }> = {
    SUCCESS: { color: "#10b981", bg: "rgba(16, 185, 129, 0.15)" },
    FAILED: { color: "#ef4444", bg: "rgba(239, 68, 68, 0.15)" },
    RUNNING: { color: "#f59e0b", bg: "rgba(245, 158, 11, 0.15)" },
  };
  const statusStyle = statusColors[detail.status] || { color: "#6b7280", bg: "rgba(107, 114, 128, 0.15)" };

  return (
    <Card>
      <Card.Body>
        <div className="trace-detail-frame">
        {/* Header section */}
        <div className="trace-detail-header">
          <div className="trace-detail-header-top">
            <div>
              <div className="trace-detail-title-row">
                <h3 className="trace-detail-title">
                  Trace: <code className="trace-detail-id">{traceId.substring(0, 8)}</code>
                </h3>
                <span
                  className="trace-detail-status"
                  style={{ "--status-color": statusStyle.color, "--status-bg": statusStyle.bg } as React.CSSProperties}
                >
                  {detail.status}
                </span>
              </div>
              <div className="trace-detail-meta">
                <span>{detail.started_at ? new Date(detail.started_at).toLocaleString() : "N/A"}</span>
                <span className="trace-detail-meta-dot">•</span>
                <span className="trace-detail-duration">{detail.duration_seconds.toFixed(2)}s</span>
              </div>
            </div>
            <div className="trace-detail-controls">
              <select
                className="ui-input trace-detail-select"
                value={level}
                onChange={(e) => setLevel(e.target.value as "summary" | "full" | "errors")}
              >
                <option value="summary">Summary</option>
                <option value="full">Full Details</option>
                <option value="errors">Errors Only</option>
              </select>
              <Button variant="ghost" size="sm" onClick={onClose}>
                Close
              </Button>
            </div>
          </div>
        </div>

        {/* Stats cards */}
        <div className="trace-detail-stats">
          {[
            { label: "Runs", value: detail.counts.runs, color: sourceStyles.run.color },
            { label: "Workers", value: detail.counts.workers, color: sourceStyles.worker.color },
            { label: "LLM Calls", value: detail.counts.llm_calls, color: sourceStyles.llm.color },
          ].map((stat) => (
            <div
              key={stat.label}
              className="trace-detail-stat"
            >
              <div className="trace-detail-stat-label">
                {stat.label}
              </div>
              <div
                className="trace-detail-stat-value"
                style={{ "--stat-color": stat.value > 0 ? stat.color : "var(--color-text-muted)" } as React.CSSProperties}
              >
                {stat.value}
              </div>
            </div>
          ))}
        </div>

        {/* Anomalies */}
        {detail.anomalies.length > 0 && (
          <div className="trace-detail-anomalies">
            <div className="trace-detail-anomalies-title">
              Anomalies Detected
            </div>
            <div className="trace-detail-anomalies-list">
              {detail.anomalies.map((a, i) => (
                <div key={i} className="trace-detail-anomaly">
                  • {a}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Timeline */}
        <div className="trace-detail-timeline">
          <div className="trace-detail-timeline-title">
            Timeline ({detail.timeline.length} events)
          </div>
          <div className="trace-detail-timeline-scroll">
            {detail.timeline.length === 0 ? (
              <div className="trace-detail-empty">
                No events to display
              </div>
            ) : (
              detail.timeline.map((event, i) => (
                <TimelineEventRow key={i} event={event} isLast={i === detail.timeline.length - 1} />
              ))
            )}
          </div>
        </div>
        </div>
      </Card.Body>
    </Card>
  );
}

export default function TraceExplorerPage() {
  const { user } = useAuth();
  const { traceId: urlTraceId } = useParams<{ traceId?: string }>();
  const navigate = useNavigate();
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const limit = 20;

  // Sync URL param to state on mount and when URL changes
  useEffect(() => {
    if (urlTraceId) {
      setSelectedTraceId(urlTraceId);
    }
  }, [urlTraceId]);

  // Update URL when trace selection changes
  const handleSelectTrace = (traceId: string | null) => {
    setSelectedTraceId(traceId);
    if (traceId) {
      navigate(`/traces/${traceId}`, { replace: true });
    } else {
      navigate('/traces', { replace: true });
    }
  };

  const { data, isLoading, error } = useQuery({
    queryKey: ["traces", limit, offset],
    queryFn: () => fetchTraces(limit, offset),
    enabled: !!user,
  });

  if (!user) {
    return <div>Loading...</div>;
  }

  const formatDuration = (ms: number | null) => {
    if (ms === null) return "-";
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  };

  return (
    <div className="trace-explorer-container">
      <SectionHeader
        title="Trace Explorer"
        description="Debug supervisor runs, workers, and LLM calls with unified trace timelines."
      />

      {selectedTraceId ? (
        <TraceDetailView traceId={selectedTraceId} onClose={() => handleSelectTrace(null)} />
      ) : (
        <Card>
          <Card.Header>
            <h3 className="trace-explorer-section-title">Recent Traces</h3>
          </Card.Header>
          <Card.Body>
            {isLoading ? (
              <div className="trace-explorer-loading">Loading traces...</div>
            ) : error ? (
              <EmptyState variant="error" title="Error" description={String(error)} />
            ) : !data || data.traces.length === 0 ? (
              <EmptyState title="No traces found" description="Traces will appear here once agents start running." />
            ) : (
              <>
                <Table>
                  <Table.Header>
                    <Table.Cell isHeader>Trace ID</Table.Cell>
                    <Table.Cell isHeader>Status</Table.Cell>
                    <Table.Cell isHeader>Model</Table.Cell>
                    <Table.Cell isHeader>Started</Table.Cell>
                    <Table.Cell isHeader>Duration</Table.Cell>
                  </Table.Header>
                  <Table.Body>
                    {data.traces.map((trace) => (
                      <Table.Row
                        key={trace.trace_id}
                        onClick={() => handleSelectTrace(trace.trace_id)}
                        className="trace-explorer-trace-row"
                      >
                        <Table.Cell>
                          <code className="trace-explorer-trace-id">{trace.trace_id.substring(0, 8)}...</code>
                        </Table.Cell>
                        <Table.Cell>
                          <Badge
                            variant={
                              trace.status === "success"
                                ? "success"
                                : trace.status === "failed"
                                ? "error"
                                : "neutral"
                            }
                          >
                            {trace.status || "unknown"}
                          </Badge>
                        </Table.Cell>
                        <Table.Cell>{trace.model || "-"}</Table.Cell>
                        <Table.Cell>
                          {trace.started_at ? new Date(trace.started_at).toLocaleString() : "-"}
                        </Table.Cell>
                        <Table.Cell>{formatDuration(trace.duration_ms)}</Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </Table>

                {/* Pagination */}
                <div className="trace-explorer-pagination">
                  <Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
                    Previous
                  </Button>
                  <span className="trace-explorer-pagination-label">
                    Showing {offset + 1}-{offset + data.count}
                  </span>
                  <Button
                    variant="ghost"
                    disabled={data.count < limit}
                    onClick={() => setOffset(offset + limit)}
                  >
                    Next
                  </Button>
                </div>
              </>
            )}
          </Card.Body>
        </Card>
      )}
    </div>
  );
}
