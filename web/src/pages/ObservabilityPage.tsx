import { type CSSProperties, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Badge,
  Card,
  EmptyState,
  PageShell,
  SectionHeader,
  Spinner,
  Table,
} from "../components/ui";
import config from "../lib/config";
import { parseUTC } from "../lib/dateUtils";
import { useReadinessFlag } from "../lib/readiness-contract";
import { formatCompactDuration } from "../lib/runnerPresentation";
import type {
  MachineHealthItemResponse,
  ManagedTurnProviderSummaryResponse,
  ObservabilityOverviewResponse,
  SlowTurnItemResponse,
} from "../services/api/types";

const OVERVIEW_MACHINE_LIMIT = 8;
const OVERVIEW_SLOW_TURN_LIMIT = 8;
const DEFAULT_SLOW_THRESHOLD_MS = 30_000;

function buildWindowLabel(hoursBack: number): string {
  if (hoursBack < 24) {
    return hoursBack === 1 ? "Last 1 Hour" : `Last ${hoursBack} Hours`;
  }
  if (hoursBack === 24) return "Last 24 Hours";
  const days = Math.round(hoursBack / 24);
  return days === 1 ? "Last 1 Day" : `Last ${days} Days`;
}

async function fetchObservabilityOverview(hoursBack: number): Promise<ObservabilityOverviewResponse> {
  const params = new URLSearchParams({
    hours_back: String(hoursBack),
    slow_threshold_ms: String(DEFAULT_SLOW_THRESHOLD_MS),
    machine_limit: String(OVERVIEW_MACHINE_LIMIT),
    slow_turn_limit: String(OVERVIEW_SLOW_TURN_LIMIT),
  });
  const response = await fetch(`${config.apiBaseUrl}/observability/overview?${params.toString()}`, {
    credentials: "include",
  });

  if (!response.ok) {
    if (response.status === 501) {
      throw new Error("Observability is only available on single-tenant runtimes right now.");
    }
    if (response.status === 403) {
      throw new Error("You do not have access to this observability surface.");
    }
    const detail = await response.text();
    throw new Error(detail || "Failed to fetch observability overview.");
  }

  return response.json();
}

function formatLatencyMs(value: number | null | undefined): string {
  if (typeof value !== "number") return "n/a";
  if (value < 1000) return `${value}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}s`;
  return formatCompactDuration(Math.round(value / 1000));
}

function formatHeartbeatAge(seconds: number): string {
  return `${formatCompactDuration(seconds)} ago`;
}

function formatGeneratedAt(value: string): string {
  return parseUTC(value).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

function machineStatusVariant(status: string): "success" | "warning" | "error" | "neutral" {
  switch (status) {
    case "healthy":
      return "success";
    case "broken":
      return "error";
    case "offline":
      return "neutral";
    default:
      return "warning";
  }
}

function providerSlowVariant(provider: ManagedTurnProviderSummaryResponse): "success" | "warning" | "neutral" {
  if (provider.slow_turns > 0) return "warning";
  if (provider.completed_turns > 0) return "success";
  return "neutral";
}

function MetricCard({
  title,
  value,
  subtitle,
  accent,
}: {
  title: string;
  value: string | number;
  subtitle?: string;
  accent: string;
}) {
  return (
    <Card className="metric-card" style={{ "--metric-accent": accent } as CSSProperties}>
      <Card.Header>
        <h4 className="metric-title">{title}</h4>
      </Card.Header>
      <Card.Body>
        <div className="metric-value">{value}</div>
        {subtitle ? <div className="metric-subtitle">{subtitle}</div> : null}
      </Card.Body>
    </Card>
  );
}

function ProviderSummaryTable({ providers }: { providers: ManagedTurnProviderSummaryResponse[] }) {
  if (providers.length === 0) {
    return (
      <EmptyState
        title="No managed turns yet"
        description="Managed turn summary will appear here once Longhouse has observed completed managed turns."
      />
    );
  }

  return (
    <Table className="observability-table">
      <Table.Header>
        <Table.Cell isHeader>Provider</Table.Cell>
        <Table.Cell isHeader>Completed</Table.Cell>
        <Table.Cell isHeader>Slow</Table.Cell>
        <Table.Cell isHeader>P95 Total</Table.Cell>
        <Table.Cell isHeader>P95 Submit→Send</Table.Cell>
      </Table.Header>
      <Table.Body>
        {providers.map((provider) => (
          <Table.Row key={provider.provider}>
            <Table.Cell>
              <div className="observability-provider-cell">
                <span className="observability-provider-name">{provider.provider}</span>
                <Badge variant={providerSlowVariant(provider)}>{provider.slow_turns} slow</Badge>
              </div>
            </Table.Cell>
            <Table.Cell>{provider.completed_turns}</Table.Cell>
            <Table.Cell>{provider.slow_turns}</Table.Cell>
            <Table.Cell>{formatLatencyMs(provider.total_turn_time_ms.p95)}</Table.Cell>
            <Table.Cell>{formatLatencyMs(provider.submit_to_send_ms.p95)}</Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
  );
}

function MachineHealthPanel({ machines }: { machines: MachineHealthItemResponse[] }) {
  if (machines.length === 0) {
    return (
      <EmptyState
        title="No machine heartbeat data"
        description="Machine transport health appears after the engine has sent heartbeats."
      />
    );
  }

  return (
    <div className="observability-machine-grid">
      {machines.map((machine) => (
        <div key={machine.device_id} className={`observability-machine-card observability-machine-card--${machine.status}`}>
          <div className="observability-machine-card__header">
            <div>
              <h4 className="observability-machine-name">{machine.device_id}</h4>
              <p className="observability-machine-version">
                {machine.version ? `v${machine.version}` : "Version unknown"}
              </p>
            </div>
            <Badge variant={machineStatusVariant(machine.status)}>{machine.status}</Badge>
          </div>
          <p className="observability-machine-summary">{machine.status_summary}</p>
          <div className="observability-machine-meta">
            <span>Heartbeat {formatHeartbeatAge(machine.heartbeat_age_seconds)}</span>
            <span>{machine.ship_attempts_1h} ship attempts (1h)</span>
          </div>
          <div className="observability-machine-stats">
            <div className="observability-machine-stat">
              <span className="observability-stat-label">Ship p95</span>
              <span className="observability-stat-value">{formatLatencyMs(machine.ship_latency_p95_ms_1h)}</span>
            </div>
            <div className="observability-machine-stat">
              <span className="observability-stat-label">Pending spool</span>
              <span className="observability-stat-value">{machine.spool_pending}</span>
            </div>
            <div className="observability-machine-stat">
              <span className="observability-stat-label">Dead spool</span>
              <span className="observability-stat-value">{machine.spool_dead}</span>
            </div>
            <div className="observability-machine-stat">
              <span className="observability-stat-label">Failures</span>
              <span className="observability-stat-value">{machine.consecutive_failures}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function SlowTurnsTable({ turns }: { turns: SlowTurnItemResponse[] }) {
  if (turns.length === 0) {
    return (
      <EmptyState
        title="No recent slow turns"
        description="Recent slow managed turns will show up here when they cross the current threshold."
      />
    );
  }

  return (
    <Table className="observability-table">
      <Table.Header>
        <Table.Cell isHeader>Provider</Table.Cell>
        <Table.Cell isHeader>Session</Table.Cell>
        <Table.Cell isHeader>Total</Table.Cell>
        <Table.Cell isHeader>Submit→Send</Table.Cell>
        <Table.Cell isHeader>Active→Terminal</Table.Cell>
        <Table.Cell isHeader>Machine</Table.Cell>
      </Table.Header>
      <Table.Body>
        {turns.map((turn) => (
          <Table.Row key={turn.turn_id}>
            <Table.Cell>
              <div className="observability-provider-stack">
                <span>{turn.provider}</span>
                {turn.project ? <span className="observability-cell-subtle">{turn.project}</span> : null}
              </div>
            </Table.Cell>
            <Table.Cell>
              <div className="observability-provider-stack">
                <span className="observability-turn-session-id">{turn.session_id.slice(0, 8)}</span>
                <span className="observability-cell-subtle">
                  {turn.device_name || turn.device_id || "Machine unknown"}
                </span>
              </div>
            </Table.Cell>
            <Table.Cell>{formatLatencyMs(turn.total_turn_time_ms)}</Table.Cell>
            <Table.Cell>{formatLatencyMs(turn.timing.submit_to_send_ms)}</Table.Cell>
            <Table.Cell>{formatLatencyMs(turn.timing.active_to_terminal_ms)}</Table.Cell>
            <Table.Cell>
              {turn.machine ? (
                <Badge variant={machineStatusVariant(turn.machine.status)}>{turn.machine.status}</Badge>
              ) : (
                <span className="observability-cell-subtle">No heartbeat</span>
              )}
            </Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
  );
}

export default function ObservabilityPage() {
  const [hoursBack, setHoursBack] = useState(24);
  const { data, isLoading, error } = useQuery({
    queryKey: ["observability-overview", hoursBack],
    queryFn: () => fetchObservabilityOverview(hoursBack),
    enabled: config.singleTenant,
    refetchInterval: 15_000,
    retry: false,
  });

  useReadinessFlag({ ready: !config.singleTenant || !isLoading });

  if (!config.singleTenant) {
    return (
      <PageShell size="wide" className="observability-page-container">
        <EmptyState
          title="Observability is single-tenant for now"
          description="This dashboard reads the self-hosted or provisioned runtime telemetry surfaces. Multi-tenant browser access is not wired yet."
        />
      </PageShell>
    );
  }

  if (isLoading) {
    return (
      <PageShell size="wide" className="observability-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading observability..."
          description="Fetching managed-turn latency and machine transport health."
        />
      </PageShell>
    );
  }

  if (error || !data) {
    return (
      <PageShell size="wide" className="observability-page-container">
        <EmptyState
          variant="error"
          title="Error loading observability"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      </PageShell>
    );
  }

  const unhealthyMachines = data.machine_counts.broken + data.machine_counts.offline + data.machine_counts.degraded;
  const blockedMachines = data.machine_counts.broken + data.machine_counts.offline;
  const visibleSlowTurnRows = Math.min(data.slow_turn_total, OVERVIEW_SLOW_TURN_LIMIT);

  return (
    <PageShell size="wide" className="observability-page-container">
      <div className="observability-page">
        <SectionHeader
          title="Observability"
          description="Managed-session latency and machine transport health, built directly into Longhouse instead of a separate telemetry stack."
          actions={
            <div className="observability-controls">
              <span className="observability-controls__label">Window</span>
              <select
                aria-label="Observability window"
                className="modal-select observability-select"
                value={hoursBack}
                onChange={(event) => setHoursBack(Number(event.target.value))}
              >
                <option value={1}>Last 1 Hour</option>
                <option value={6}>Last 6 Hours</option>
                <option value={24}>Last 24 Hours</option>
                <option value={168}>Last 7 Days</option>
              </select>
              <span className="observability-controls__stamp">Updated {formatGeneratedAt(data.generated_at)}</span>
            </div>
          }
        />

        <div className="observability-stack">
          <div className="observability-note">
            <strong>{buildWindowLabel(data.hours_back)}</strong>
            <span>
              Same API surface powers this page and future agent debugging flows. Slow turns currently mean total
              turn time of at least {formatLatencyMs(data.slow_threshold_ms)}.
            </span>
          </div>

          <div className="observability-metrics-grid">
            <MetricCard
              title="Completed Turns"
              value={data.summary.completed_turns}
              subtitle={buildWindowLabel(data.hours_back)}
              accent="var(--color-brand-primary)"
            />
            <MetricCard
              title="Slow Turns"
              value={data.summary.slow_turns}
              subtitle={`Showing ${visibleSlowTurnRows} of ${data.slow_turn_total} slow-turn rows`}
              accent="var(--color-intent-warning)"
            />
            <MetricCard
              title="P95 Total Turn"
              value={formatLatencyMs(data.summary.total_turn_time_ms.p95)}
              subtitle={`Max ${formatLatencyMs(data.summary.total_turn_time_ms.max)}`}
              accent="var(--color-brand-secondary)"
            />
            <MetricCard
              title="P95 Submit→Send"
              value={formatLatencyMs(data.summary.submit_to_send_ms.p95)}
              subtitle="Runtime dispatch overhead"
              accent="var(--color-intent-success)"
            />
            <MetricCard
              title="Unhealthy Machines"
              value={`${unhealthyMachines}/${data.machine_counts.total}`}
              subtitle={`${data.machine_counts.healthy} currently healthy`}
              accent="var(--color-intent-warning)"
            />
            <MetricCard
              title="Broken Or Offline"
              value={blockedMachines}
              subtitle={`${data.machine_counts.broken} broken, ${data.machine_counts.offline} offline`}
              accent="var(--color-intent-error)"
            />
          </div>

          <div className="observability-grid">
            <Card className="observability-panel">
              <Card.Header className="observability-panel__header">
                <div>
                  <h3 className="observability-panel__title">Provider Drift</h3>
                  <p className="observability-panel__description">
                    Per-provider latency splits so regressions are visible without log archaeology.
                  </p>
                </div>
              </Card.Header>
              <Card.Body>
                <ProviderSummaryTable providers={data.providers} />
              </Card.Body>
            </Card>

            <Card className="observability-panel">
              <Card.Header className="observability-panel__header">
                <div>
                  <h3 className="observability-panel__title">Machine Health</h3>
                  <p className="observability-panel__description">
                    Latest heartbeat-derived transport state from the shipping path on each machine.
                  </p>
                </div>
              </Card.Header>
              <Card.Body>
                <MachineHealthPanel machines={data.machines} />
              </Card.Body>
            </Card>
          </div>

          <Card className="observability-panel">
            <Card.Header className="observability-panel__header">
              <div>
                <h3 className="observability-panel__title">Recent Slow Turns</h3>
                <p className="observability-panel__description">
                  Slowest recent managed turns across sessions, already enriched with current machine state.
                </p>
              </div>
            </Card.Header>
            <Card.Body>
              <SlowTurnsTable turns={data.slow_turns} />
            </Card.Body>
          </Card>
        </div>
      </div>
    </PageShell>
  );
}
