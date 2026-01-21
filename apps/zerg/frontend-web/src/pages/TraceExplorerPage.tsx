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
    <div style={{ display: "flex", gap: "var(--space-4)", minHeight: "60px" }}>
      {/* Timestamp column */}
      <div style={{
        width: "110px",
        flexShrink: 0,
        paddingTop: "2px",
        fontSize: "0.8rem",
        fontFamily: "var(--font-mono, monospace)",
        color: "var(--color-text-muted)",
        letterSpacing: "-0.02em"
      }}>
        {formatTime(event.timestamp)}
      </div>

      {/* Timeline indicator */}
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", width: "20px", flexShrink: 0 }}>
        <div
          style={{
            width: "12px",
            height: "12px",
            borderRadius: "50%",
            backgroundColor: event.is_error ? "#ef4444" : style.color,
            boxShadow: event.is_error
              ? "0 0 0 3px rgba(239, 68, 68, 0.2)"
              : `0 0 0 3px ${style.bg}`,
            flexShrink: 0,
          }}
        />
        {!isLast && (
          <div style={{
            width: "2px",
            flex: 1,
            backgroundColor: "var(--color-border)",
            marginTop: "4px"
          }} />
        )}
      </div>

      {/* Content */}
      <div style={{ flex: 1, paddingBottom: "var(--space-4)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", flexWrap: "wrap" }}>
          {/* Event name */}
          <span style={{ fontWeight: 600, fontSize: "0.9rem" }}>{event.event_type}</span>

          {/* Source badge */}
          <span style={{
            fontSize: "0.65rem",
            fontWeight: 600,
            padding: "2px 8px",
            borderRadius: "4px",
            backgroundColor: style.bg,
            color: style.color,
            letterSpacing: "0.03em",
          }}>
            {style.label}
          </span>

          {/* Duration */}
          {event.duration_ms != null && event.duration_ms > 0 && (
            <span style={{
              fontSize: "0.8rem",
              color: "var(--color-text-muted)",
              fontFamily: "var(--font-mono, monospace)"
            }}>
              {formatDuration(event.duration_ms)}
            </span>
          )}

          {/* Error badge */}
          {event.is_error && (
            <span style={{
              fontSize: "0.65rem",
              fontWeight: 600,
              padding: "2px 8px",
              borderRadius: "4px",
              backgroundColor: "rgba(239, 68, 68, 0.15)",
              color: "#ef4444",
            }}>
              ERROR
            </span>
          )}
        </div>

        {/* Detail pills */}
        {detailPills.length > 0 && (
          <div style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "6px",
            marginTop: "var(--space-2)"
          }}>
            {detailPills.map(({ key, value }) => (
              <span
                key={key}
                style={{
                  fontSize: "0.75rem",
                  padding: "3px 8px",
                  borderRadius: "6px",
                  backgroundColor: "rgba(255, 255, 255, 0.05)",
                  border: "1px solid var(--color-border)",
                  color: "var(--color-text-muted)",
                }}
              >
                <span style={{ color: "var(--color-text-secondary)" }}>{key}</span>
                <span style={{ opacity: 0.5, margin: "0 4px" }}>=</span>
                <span style={{ color: "var(--color-text)" }}>{value}</span>
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
          <div style={{ textAlign: "center", padding: "var(--space-8)" }}>Loading trace...</div>
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
        <div style={{ margin: "calc(-1 * var(--space-4))" }}>
        {/* Header section */}
        <div style={{
          padding: "var(--space-5) var(--space-6)",
          borderBottom: "1px solid var(--color-border)",
          background: "linear-gradient(to bottom, rgba(255,255,255,0.02), transparent)"
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", marginBottom: "var(--space-2)" }}>
                <h3 style={{ margin: 0, fontSize: "1.1rem", fontWeight: 600 }}>
                  Trace: <code style={{ fontFamily: "var(--font-mono, monospace)", fontSize: "0.95rem" }}>{traceId.substring(0, 8)}</code>
                </h3>
                <span style={{
                  fontSize: "0.7rem",
                  fontWeight: 600,
                  padding: "3px 10px",
                  borderRadius: "12px",
                  backgroundColor: statusStyle.bg,
                  color: statusStyle.color,
                  letterSpacing: "0.03em",
                }}>
                  {detail.status}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "var(--space-4)", fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                <span>{detail.started_at ? new Date(detail.started_at).toLocaleString() : "N/A"}</span>
                <span style={{ opacity: 0.3 }}>•</span>
                <span style={{ fontFamily: "var(--font-mono, monospace)" }}>{detail.duration_seconds.toFixed(2)}s</span>
              </div>
            </div>
            <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
              <select
                className="ui-input"
                value={level}
                onChange={(e) => setLevel(e.target.value as "summary" | "full" | "errors")}
                style={{
                  width: "auto",
                  height: "32px",
                  fontSize: "0.8rem",
                  backgroundColor: "rgba(255,255,255,0.05)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "0 var(--space-3)",
                }}
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
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: "1px",
          backgroundColor: "var(--color-border)",
          borderBottom: "1px solid var(--color-border)"
        }}>
          {[
            { label: "Runs", value: detail.counts.runs, color: sourceStyles.run.color },
            { label: "Workers", value: detail.counts.workers, color: sourceStyles.worker.color },
            { label: "LLM Calls", value: detail.counts.llm_calls, color: sourceStyles.llm.color },
          ].map((stat) => (
            <div
              key={stat.label}
              style={{
                padding: "var(--space-4) var(--space-5)",
                backgroundColor: "var(--color-bg-secondary)",
                textAlign: "center",
              }}
            >
              <div style={{
                fontSize: "0.7rem",
                fontWeight: 500,
                color: "var(--color-text-muted)",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
                marginBottom: "var(--space-1)"
              }}>
                {stat.label}
              </div>
              <div style={{
                fontSize: "1.75rem",
                fontWeight: 700,
                color: stat.value > 0 ? stat.color : "var(--color-text-muted)",
                fontFamily: "var(--font-mono, monospace)",
              }}>
                {stat.value}
              </div>
            </div>
          ))}
        </div>

        {/* Anomalies */}
        {detail.anomalies.length > 0 && (
          <div style={{
            padding: "var(--space-4) var(--space-6)",
            borderBottom: "1px solid var(--color-border)",
            backgroundColor: "rgba(239, 68, 68, 0.05)"
          }}>
            <div style={{
              fontSize: "0.7rem",
              fontWeight: 600,
              color: "#ef4444",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              marginBottom: "var(--space-2)"
            }}>
              Anomalies Detected
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
              {detail.anomalies.map((a, i) => (
                <div key={i} style={{ fontSize: "0.85rem", color: "var(--color-text)" }}>
                  • {a}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Timeline */}
        <div style={{ padding: "var(--space-5) var(--space-6)" }}>
          <div style={{
            fontSize: "0.7rem",
            fontWeight: 600,
            color: "var(--color-text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
            marginBottom: "var(--space-4)"
          }}>
            Timeline ({detail.timeline.length} events)
          </div>
          <div style={{ maxHeight: "450px", overflowY: "auto", paddingRight: "var(--space-2)" }}>
            {detail.timeline.length === 0 ? (
              <div style={{ textAlign: "center", color: "var(--color-text-muted)", padding: "var(--space-8)" }}>
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
    <div className="trace-explorer-container" style={{ padding: "var(--space-6)" }}>
      <SectionHeader
        title="Trace Explorer"
        description="Debug supervisor runs, workers, and LLM calls with unified trace timelines."
      />

      {selectedTraceId ? (
        <TraceDetailView traceId={selectedTraceId} onClose={() => handleSelectTrace(null)} />
      ) : (
        <Card>
          <Card.Header>
            <h3 style={{ margin: 0 }}>Recent Traces</h3>
          </Card.Header>
          <Card.Body>
            {isLoading ? (
              <div style={{ textAlign: "center", padding: "var(--space-8)" }}>Loading traces...</div>
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
                        style={{ cursor: "pointer" }}
                      >
                        <Table.Cell>
                          <code style={{ fontSize: "0.75rem" }}>{trace.trace_id.substring(0, 8)}...</code>
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
                <div style={{ display: "flex", justifyContent: "center", gap: "var(--space-2)", marginTop: "var(--space-4)" }}>
                  <Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
                    Previous
                  </Button>
                  <span style={{ padding: "var(--space-2)", color: "var(--color-text-muted)" }}>
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
