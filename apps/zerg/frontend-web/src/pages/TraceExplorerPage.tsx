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

// Timeline event component
function TimelineEventRow({ event }: { event: TimelineEvent }) {
  const sourceColors: Record<string, string> = {
    run: "#3b82f6",
    worker: "#10b981",
    llm: "#8b5cf6",
  };

  const formatTime = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      fractionalSecondDigits: 3,
    });
  };

  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: "var(--space-4)",
        padding: "var(--space-3) 0",
        borderBottom: "1px solid var(--color-border)",
      }}
    >
      <div style={{ width: "100px", fontSize: "0.75rem", color: "var(--color-text-muted)" }}>
        {formatTime(event.timestamp)}
      </div>
      <div
        style={{
          width: "8px",
          height: "8px",
          borderRadius: "50%",
          backgroundColor: event.is_error ? "#ef4444" : sourceColors[event.source] || "#6b7280",
          marginTop: "4px",
          flexShrink: 0,
        }}
      />
      <div style={{ flex: 1 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <span style={{ fontWeight: 500 }}>{event.event_type}</span>
          <Badge variant={event.source === "run" ? "neutral" : event.source === "worker" ? "success" : "warning"}>
            {event.source}
          </Badge>
          {event.duration_ms && (
            <span style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>
              {event.duration_ms}ms
            </span>
          )}
          {event.is_error && <Badge variant="error">ERROR</Badge>}
        </div>
        <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", marginTop: "var(--space-1)" }}>
          {Object.entries(event.details)
            .filter(([_, v]) => v != null)
            .map(([k, v]) => `${k}=${String(v).substring(0, 50)}`)
            .join(" ")}
        </div>
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

  return (
    <Card>
      <Card.Header>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <h3 style={{ margin: 0 }}>Trace: {traceId.substring(0, 8)}...</h3>
            <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", marginTop: "var(--space-1)" }}>
              {detail.started_at ? new Date(detail.started_at).toLocaleString() : "N/A"} |{" "}
              Duration: {detail.duration_seconds.toFixed(2)}s |{" "}
              <Badge variant={detail.status === "SUCCESS" ? "success" : detail.status === "FAILED" ? "error" : "neutral"}>
                {detail.status}
              </Badge>
            </div>
          </div>
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            <select
              className="ui-input"
              value={level}
              onChange={(e) => setLevel(e.target.value as "summary" | "full" | "errors")}
              style={{ width: "auto", height: "32px" }}
            >
              <option value="summary">Summary</option>
              <option value="full">Full Details</option>
              <option value="errors">Errors Only</option>
            </select>
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>
      </Card.Header>
      <Card.Body>
        {/* Counts */}
        <div style={{ display: "flex", gap: "var(--space-6)", marginBottom: "var(--space-6)" }}>
          <div>
            <span style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>Runs</span>
            <div style={{ fontSize: "1.5rem", fontWeight: 700 }}>{detail.counts.runs}</div>
          </div>
          <div>
            <span style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>Workers</span>
            <div style={{ fontSize: "1.5rem", fontWeight: 700 }}>{detail.counts.workers}</div>
          </div>
          <div>
            <span style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>LLM Calls</span>
            <div style={{ fontSize: "1.5rem", fontWeight: 700 }}>{detail.counts.llm_calls}</div>
          </div>
        </div>

        {/* Anomalies */}
        {detail.anomalies.length > 0 && (
          <div style={{ marginBottom: "var(--space-6)" }}>
            <h4 style={{ margin: "0 0 var(--space-3) 0" }}>Anomalies</h4>
            <ul style={{ margin: 0, paddingLeft: "var(--space-4)" }}>
              {detail.anomalies.map((a, i) => (
                <li key={i} style={{ color: "#ef4444", fontSize: "0.875rem" }}>
                  {a}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Timeline */}
        <h4 style={{ margin: "0 0 var(--space-3) 0" }}>Timeline ({detail.timeline.length} events)</h4>
        <div style={{ maxHeight: "400px", overflowY: "auto" }}>
          {detail.timeline.length === 0 ? (
            <div style={{ textAlign: "center", color: "var(--color-text-muted)", padding: "var(--space-6)" }}>
              No events to display
            </div>
          ) : (
            detail.timeline.map((event, i) => <TimelineEventRow key={i} event={event} />)
          )}
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
