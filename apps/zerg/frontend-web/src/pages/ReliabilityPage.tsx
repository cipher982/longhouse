import React from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../lib/auth";
import config from "../lib/config";
import {
  Button,
  Card,
  SectionHeader,
  EmptyState,
  Table,
  Badge,
  PageShell
} from "../components/ui";

// Types for reliability data
interface SystemHealthResponse {
  workers: Record<string, number>;
  recent_run_errors: number;
  recent_worker_errors: number;
  status: "healthy" | "degraded" | "unhealthy";
  checked_at: string;
}

interface ErrorsResponse {
  run_errors: Array<{
    id: number;
    error: string | null;
    created_at: string | null;
    trace_id: string | null;
  }>;
  worker_errors: Array<{
    id: number;
    error: string | null;
    created_at: string | null;
    task_preview: string | null;
    trace_id: string | null;
  }>;
  total_run_errors: number;
  total_worker_errors: number;
  hours: number;
}

interface PerformanceResponse {
  p50: number | null;
  p95: number | null;
  p99: number | null;
  count: number;
  min: number | null;
  max: number | null;
  hours: number;
}

interface StuckWorkersResponse {
  stuck_count: number;
  threshold_mins: number;
  workers: Array<{
    id: number;
    task: string | null;
    started_at: string | null;
    worker_id: string | null;
    trace_id: string | null;
  }>;
}

interface RunnerStatusResponse {
  total: number;
  runners: Array<{
    id: number;
    name: string;
    status: string;
    last_seen_at: string | null;
    capabilities: string[];
  }>;
}

// API functions
async function fetchSystemHealth(): Promise<SystemHealthResponse> {
  const response = await fetch(`${config.apiBaseUrl}/reliability/system-health`, {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(response.status === 403 ? "Admin access required" : "Failed to fetch system health");
  }
  return response.json();
}

async function fetchErrors(hours: number = 24): Promise<ErrorsResponse> {
  const response = await fetch(`${config.apiBaseUrl}/reliability/errors?hours=${hours}`, {
    credentials: "include",
  });
  if (!response.ok) throw new Error("Failed to fetch errors");
  return response.json();
}

async function fetchPerformance(hours: number = 24): Promise<PerformanceResponse> {
  const response = await fetch(`${config.apiBaseUrl}/reliability/performance?hours=${hours}`, {
    credentials: "include",
  });
  if (!response.ok) throw new Error("Failed to fetch performance");
  return response.json();
}

async function fetchStuckWorkers(): Promise<StuckWorkersResponse> {
  const response = await fetch(`${config.apiBaseUrl}/reliability/workers/stuck`, {
    credentials: "include",
  });
  if (!response.ok) throw new Error("Failed to fetch stuck workers");
  return response.json();
}

async function fetchRunnerStatus(): Promise<RunnerStatusResponse> {
  const response = await fetch(`${config.apiBaseUrl}/reliability/runners`, {
    credentials: "include",
  });
  if (!response.ok) throw new Error("Failed to fetch runner status");
  return response.json();
}

// Metric card component
function MetricCard({
  title,
  value,
  subtitle,
  color = "#10b981",
}: {
  title: string;
  value: string | number;
  subtitle?: string;
  color?: string;
}) {
  return (
    <Card className="metric-card" style={{ "--metric-accent": color } as React.CSSProperties}>
      <Card.Header>
        <h4 className="metric-title">{title}</h4>
      </Card.Header>
      <Card.Body>
        <div className="metric-value">{value}</div>
        {subtitle && <div className="metric-subtitle">{subtitle}</div>}
      </Card.Body>
    </Card>
  );
}

// Status indicator component
function StatusIndicator({ status }: { status: "healthy" | "degraded" | "unhealthy" }) {
  const colors = {
    healthy: "#10b981",
    degraded: "#f59e0b",
    unhealthy: "#ef4444",
  };

  return (
    <div className="reliability-status" style={{ "--status-color": colors[status] } as React.CSSProperties}>
      <div
        className={`reliability-status-dot${status !== "healthy" ? " reliability-status-dot--pulse" : ""}`}
      />
      <span className="reliability-status-label">{status}</span>
    </div>
  );
}

export default function ReliabilityPage() {
  const { user } = useAuth();

  // Queries
  const { data: health, isLoading: healthLoading, error: healthError } = useQuery({
    queryKey: ["reliability-health"],
    queryFn: fetchSystemHealth,
    refetchInterval: 30000,
    enabled: !!user,
  });

  const { data: errors } = useQuery({
    queryKey: ["reliability-errors"],
    queryFn: () => fetchErrors(24),
    refetchInterval: 60000,
    enabled: !!user,
  });

  const { data: performance } = useQuery({
    queryKey: ["reliability-performance"],
    queryFn: () => fetchPerformance(24),
    refetchInterval: 60000,
    enabled: !!user,
  });

  const { data: stuckWorkers } = useQuery({
    queryKey: ["reliability-stuck"],
    queryFn: fetchStuckWorkers,
    refetchInterval: 30000,
    enabled: !!user,
  });

  const { data: runners } = useQuery({
    queryKey: ["reliability-runners"],
    queryFn: fetchRunnerStatus,
    refetchInterval: 30000,
    enabled: !!user,
  });

  if (!user) {
    return <div>Loading...</div>;
  }

  if (healthLoading) {
    return (
      <PageShell size="wide" className="reliability-page-container">
        <SectionHeader title="Reliability Dashboard" description="Monitor system health and performance." />
        <EmptyState
          icon={<div className="spinner" style={{ width: 40, height: 40 }} />}
          title="Loading..."
          description="Fetching reliability metrics."
        />
      </PageShell>
    );
  }

  if (healthError) {
    return (
      <PageShell size="wide" className="reliability-page-container">
        <SectionHeader title="Reliability Dashboard" description="Monitor system health and performance." />
        <EmptyState
          variant="error"
          title="Error loading data"
          description={String(healthError)}
          action={<Button onClick={() => window.location.reload()}>Retry</Button>}
        />
      </PageShell>
    );
  }

  return (
    <PageShell size="wide" className="reliability-page-container">
      <SectionHeader
        title="Reliability Dashboard"
        description="Monitor system health, errors, and performance metrics."
        actions={health && <StatusIndicator status={health.status} />}
      />

      <div className="reliability-stack">
        {/* Key Metrics */}
        <div className="metrics-grid reliability-metrics-grid">
          <MetricCard
            title="Run Errors (1h)"
            value={health?.recent_run_errors ?? 0}
            color={health && health.recent_run_errors > 5 ? "#ef4444" : "#10b981"}
          />
          <MetricCard
            title="Worker Errors (1h)"
            value={health?.recent_worker_errors ?? 0}
            color={health && health.recent_worker_errors > 5 ? "#ef4444" : "#10b981"}
          />
          <MetricCard
            title="P50 Latency"
            value={performance?.p50 ? `${performance.p50}ms` : "N/A"}
            color="#3b82f6"
          />
          <MetricCard
            title="P95 Latency"
            value={performance?.p95 ? `${performance.p95}ms` : "N/A"}
            subtitle={performance?.p99 ? `P99: ${performance.p99}ms` : undefined}
            color="#8b5cf6"
          />
          <MetricCard
            title="Runners Online"
            value={health?.workers?.online ?? 0}
            subtitle={`${health?.workers?.offline ?? 0} offline`}
            color="#10b981"
          />
          <MetricCard
            title="Stuck Workers"
            value={stuckWorkers?.stuck_count ?? 0}
            subtitle={`>${stuckWorkers?.threshold_mins ?? 10}min threshold`}
            color={stuckWorkers && stuckWorkers.stuck_count > 0 ? "#f59e0b" : "#10b981"}
          />
        </div>

        {/* Recent Errors */}
        <Card>
          <Card.Header>
            <h3 className="reliability-section-title">Recent Errors (24h)</h3>
          </Card.Header>
          <Card.Body>
            {errors && (errors.total_run_errors > 0 || errors.total_worker_errors > 0) ? (
              <div className="reliability-section-stack">
                {errors.run_errors.length > 0 && (
                  <div>
                    <h4 className="reliability-subsection-title">
                      Run Errors ({errors.total_run_errors})
                    </h4>
                    <Table>
                      <Table.Header>
                        <Table.Cell isHeader>ID</Table.Cell>
                        <Table.Cell isHeader>Error</Table.Cell>
                        <Table.Cell isHeader>Time</Table.Cell>
                        <Table.Cell isHeader>Trace</Table.Cell>
                      </Table.Header>
                      <Table.Body>
                        {errors.run_errors.slice(0, 5).map((err) => (
                          <Table.Row key={err.id}>
                            <Table.Cell>{err.id}</Table.Cell>
                            <Table.Cell>
                              <span className="reliability-small-text">{err.error || "Unknown error"}</span>
                            </Table.Cell>
                            <Table.Cell>
                              {err.created_at ? new Date(err.created_at).toLocaleString() : "-"}
                            </Table.Cell>
                            <Table.Cell>
                              {err.trace_id ? (
                                <code className="reliability-code">{err.trace_id.substring(0, 8)}...</code>
                              ) : (
                                "-"
                              )}
                            </Table.Cell>
                          </Table.Row>
                        ))}
                      </Table.Body>
                    </Table>
                  </div>
                )}

                {errors.worker_errors.length > 0 && (
                  <div>
                    <h4 className="reliability-subsection-title reliability-subsection-title--spaced">
                      Worker Errors ({errors.total_worker_errors})
                    </h4>
                    <Table>
                      <Table.Header>
                        <Table.Cell isHeader>ID</Table.Cell>
                        <Table.Cell isHeader>Task</Table.Cell>
                        <Table.Cell isHeader>Error</Table.Cell>
                        <Table.Cell isHeader>Time</Table.Cell>
                      </Table.Header>
                      <Table.Body>
                        {errors.worker_errors.slice(0, 5).map((err) => (
                          <Table.Row key={err.id}>
                            <Table.Cell>{err.id}</Table.Cell>
                            <Table.Cell>
                              <span className="reliability-small-text">{err.task_preview || "-"}</span>
                            </Table.Cell>
                            <Table.Cell>
                              <span className="reliability-small-text">{err.error || "Unknown error"}</span>
                            </Table.Cell>
                            <Table.Cell>
                              {err.created_at ? new Date(err.created_at).toLocaleString() : "-"}
                            </Table.Cell>
                          </Table.Row>
                        ))}
                      </Table.Body>
                    </Table>
                  </div>
                )}
              </div>
            ) : (
              <EmptyState title="No recent errors" description="System is running smoothly!" />
            )}
          </Card.Body>
        </Card>

        {/* Stuck Workers */}
        {stuckWorkers && stuckWorkers.stuck_count > 0 && (
          <Card>
            <Card.Header>
              <h3 className="reliability-warning-title">Stuck Workers ({stuckWorkers.stuck_count})</h3>
            </Card.Header>
            <Card.Body>
              <Table>
                <Table.Header>
                  <Table.Cell isHeader>ID</Table.Cell>
                  <Table.Cell isHeader>Task</Table.Cell>
                  <Table.Cell isHeader>Started</Table.Cell>
                  <Table.Cell isHeader>Worker ID</Table.Cell>
                </Table.Header>
                <Table.Body>
                  {stuckWorkers.workers.map((worker) => (
                    <Table.Row key={worker.id}>
                      <Table.Cell>{worker.id}</Table.Cell>
                      <Table.Cell>
                        <span className="reliability-small-text">{worker.task || "-"}</span>
                      </Table.Cell>
                      <Table.Cell>
                        {worker.started_at ? new Date(worker.started_at).toLocaleString() : "-"}
                      </Table.Cell>
                      <Table.Cell>
                        <code className="reliability-code">{worker.worker_id || "-"}</code>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            </Card.Body>
          </Card>
        )}

        {/* Runners */}
        <Card>
          <Card.Header>
            <h3 className="reliability-section-title">Runners ({runners?.total ?? 0})</h3>
          </Card.Header>
          <Card.Body>
            {runners && runners.runners.length > 0 ? (
              <Table>
                <Table.Header>
                  <Table.Cell isHeader>Name</Table.Cell>
                  <Table.Cell isHeader>Status</Table.Cell>
                  <Table.Cell isHeader>Last Seen</Table.Cell>
                  <Table.Cell isHeader>Capabilities</Table.Cell>
                </Table.Header>
                <Table.Body>
                  {runners.runners.map((runner) => (
                    <Table.Row key={runner.id}>
                      <Table.Cell>{runner.name}</Table.Cell>
                      <Table.Cell>
                        <Badge
                          variant={
                            runner.status === "online"
                              ? "success"
                              : runner.status === "offline"
                              ? "neutral"
                              : "error"
                          }
                        >
                          {runner.status}
                        </Badge>
                      </Table.Cell>
                      <Table.Cell>
                        {runner.last_seen_at ? new Date(runner.last_seen_at).toLocaleString() : "Never"}
                      </Table.Cell>
                      <Table.Cell>
                        <span className="reliability-small-text">
                          {runner.capabilities?.join(", ") || "-"}
                        </span>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            ) : (
              <EmptyState title="No runners" description="Register runners to enable remote execution." />
            )}
          </Card.Body>
        </Card>
      </div>
    </PageShell>
  );
}
