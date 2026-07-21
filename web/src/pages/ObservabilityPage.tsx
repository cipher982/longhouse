import { type CSSProperties, useCallback, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
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
import { buildSessionDetailPath, toTitleCaseWords } from "../lib/sessionUtils";
import type {
  MachineHealthItemResponse,
  ManagedTurnProviderSummaryResponse,
  ObservabilityOverviewResponse,
  ProductHealthCheckListResponse,
  ProductHealthCheckSummaryResponse,
  SlowTurnItemResponse,
} from "../services/api/types";
import { ProviderGlyph } from "../components/ProviderGlyph";

const OVERVIEW_MACHINE_LIMIT = 8;
const OVERVIEW_SLOW_TURN_LIMIT = 8;
const DEFAULT_SLOW_THRESHOLD_MS = 30_000;
const PRODUCT_HEALTH_WINDOW = "15m";

type DiagnosisTone = "success" | "warning" | "error" | "neutral";
type StatusBadgeVariant = "success" | "warning" | "error" | "neutral";

interface DiagnosisCardData {
  key: string;
  eyebrow: string;
  title: string;
  description: string;
  tone: DiagnosisTone;
  to?: string;
  ctaLabel?: string;
  onAction?: () => void;
}

function buildWindowLabel(hoursBack: number): string {
  if (hoursBack < 24) {
    return hoursBack === 1 ? "Last 1 Hour" : `Last ${hoursBack} Hours`;
  }
  if (hoursBack === 24) return "Last 24 Hours";
  const days = Math.round(hoursBack / 24);
  return days === 1 ? "Last 1 Day" : `Last ${days} Days`;
}

function formatByteSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** unitIndex;
  return `${value >= 10 || unitIndex === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`;
}

type HistoryImportSnapshot = NonNullable<MachineHealthItemResponse["history_import"]>;

function historyImportStateLabel(state: HistoryImportSnapshot["state"]): string {
  switch (state) {
    case "blocked_source":
      return "source blocked";
    case "backpressured":
      return "backpressured";
    case "inventory_ready":
      return "inventory ready";
    default:
      return state;
  }
}

function historyImportVariant(state: HistoryImportSnapshot["state"]): StatusBadgeVariant {
  if (state === "blocked_source" || state === "offline") return "error";
  if (state === "paused" || state === "backpressured") return "warning";
  if (state === "current") return "success";
  return "neutral";
}

function HistoryImportPanel({ historyImport }: { historyImport: HistoryImportSnapshot | undefined }) {
  if (historyImport?.state === "discovering") {
    return <p className="observability-history-inventory__note">Scanning local transcript sources…</p>;
  }
  if (!historyImport?.inventory) return null;

  const { inventory, progress } = historyImport;
  const progressProviders = progress?.providers ?? [];
  const inventoryProviders = inventory.providers ?? [];
  return (
    <div className="observability-history-inventory">
      <div className="observability-history-inventory__header">
        <span className="observability-stat-label">History import</span>
        <Badge
          variant={inventory.scan_error_count > 0 ? "warning" : historyImportVariant(historyImport.state)}
        >
          {inventory.scan_error_count > 0
            ? `${inventory.scan_error_count} scan errors`
            : historyImportStateLabel(historyImport.state)}
        </Badge>
      </div>
      <strong>
        {inventory.source_count.toLocaleString()} sources · {formatByteSize(inventory.footprint_bytes)} on disk
      </strong>
      {progress ? (
        <div className="observability-history-inventory__note">
          {progress.acknowledged_source_bytes + progress.remaining_source_bytes > 0 ? (
            <span>
              File logs: {formatByteSize(progress.acknowledged_source_bytes)} acknowledged ·{" "}
              {formatByteSize(progress.remaining_source_bytes)} remaining
            </span>
          ) : null}
          {progress.acknowledged_records + progress.remaining_records > 0 ? (
            <span>
              SQLite sources: {progress.acknowledged_records.toLocaleString()} records acknowledged ·{" "}
              {progress.remaining_records.toLocaleString()} known remaining
            </span>
          ) : null}
          {progress.pending_outbox_count > 0 ? (
            <span>
              {progress.pending_outbox_count.toLocaleString()} durable upload{" "}
              {progress.pending_outbox_count === 1 ? "receipt" : "receipts"} pending
            </span>
          ) : null}
          {progress.blocked_source_count > 0 ? (
            <span>
              {progress.blocked_source_count.toLocaleString()} blocked source
              {progress.blocked_source_count === 1 ? "" : "s"}
              {progress.latest_block_kind ? ` · ${toTitleCaseWords(progress.latest_block_kind)}` : ""}
            </span>
          ) : null}
        </div>
      ) : (
        <span className="observability-history-inventory__note">
          Discovery is complete. Receipt progress requires a newer Machine Agent.
        </span>
      )}
      <div className="observability-history-inventory__providers">
        {progressProviders.length > 0
          ? progressProviders.map((provider) => (
              <span key={provider.provider}>
                {toTitleCaseWords(provider.provider)}{" "}
                {provider.unit === "bytes"
                  ? `${formatByteSize(provider.acknowledged_units)} / ${formatByteSize(provider.observed_units)}`
                  : provider.unit === "records"
                    ? provider.tracked_source_count === 0 && !provider.inventory_coverage_complete
                      ? "Record discovery pending"
                      : `${provider.acknowledged_units.toLocaleString()} records · ${provider.remaining_units.toLocaleString()} known remaining${provider.inventory_coverage_complete ? "" : " · discovery in progress"}`
                    : `${provider.tracked_source_count.toLocaleString()} tracked`}
              </span>
            ))
          : inventoryProviders.map((provider) => (
              <span key={provider.provider}>
                {toTitleCaseWords(provider.provider)} {provider.source_count.toLocaleString()}
              </span>
            ))}
      </div>
    </div>
  );
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
      throw new Error("Health is only available on single-tenant runtimes right now.");
    }
    if (response.status === 403) {
      throw new Error("You do not have access to this health surface.");
    }
    const detail = await response.text();
    throw new Error(detail || "Failed to fetch the current health snapshot.");
  }

  return response.json();
}

async function fetchProductHealthChecks(): Promise<ProductHealthCheckListResponse> {
  const params = new URLSearchParams({
    window: PRODUCT_HEALTH_WINDOW,
  });
  const response = await fetch(`${config.apiBaseUrl}/observability/checks?${params.toString()}`, {
    credentials: "include",
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || "Failed to fetch product health checks.");
  }

  return response.json();
}

function buildTimelineSlicePath(filters: { provider?: string; project?: string; deviceId?: string }): string {
  const params = new URLSearchParams();
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.project) params.set("project", filters.project);
  if (filters.deviceId) params.set("device_id", filters.deviceId);
  const query = params.toString();
  return `/timeline${query ? `?${query}` : ""}`;
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

function machineStatusVariant(status: string): DiagnosisTone {
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

function providerStatusBadge(provider: ManagedTurnProviderSummaryResponse): {
  label: string;
  variant: Exclude<StatusBadgeVariant, "error">;
} {
  if (provider.slow_turns > 0) {
    return {
      label: `${provider.slow_turns} slow`,
      variant: "warning",
    };
  }
  if (provider.completed_turns > 0) {
    return {
      label: "Stable",
      variant: "success",
    };
  }
  return {
    label: "Idle",
    variant: "neutral",
  };
}

function diagnosisToneBadgeVariant(tone: DiagnosisTone): StatusBadgeVariant {
  switch (tone) {
    case "error":
      return "error";
    case "warning":
      return "warning";
    case "success":
      return "success";
    default:
      return "neutral";
  }
}

function productCheckBadgeVariant(verdict: string): StatusBadgeVariant {
  switch (verdict) {
    case "ok":
      return "success";
    case "degraded":
      return "warning";
    case "failing":
      return "error";
    default:
      return "neutral";
  }
}

function productCheckTitle(check: string): string {
  switch (check) {
    case "machine_connected":
      return "Machine Connected";
    case "render_freshness":
      return "Render Freshness";
    case "live_preview":
      return "Live Preview";
    default:
      return toTitleCaseWords(check.replace(/_/g, " "));
  }
}

function productCheckMeta(check: ProductHealthCheckSummaryResponse): string {
  const coverage = check.coverage === "full" ? "full coverage" : check.coverage === "partial" ? "partial coverage" : "no coverage";
  return `${check.window} · ${coverage}`;
}

function buildDiagnosisCards(
  data: ObservabilityOverviewResponse,
  callbacks: { scrollToSlowTurns: () => void },
): DiagnosisCardData[] {
  const cards: DiagnosisCardData[] = [];
  const blockedMachines = data.machine_counts.broken + data.machine_counts.offline;
  const degradedMachines = data.machine_counts.degraded;
  const unhealthyMachine = data.machines.find((machine) => machine.status !== "healthy");
  const slowProviders = [...data.providers]
    .filter((provider) => provider.completed_turns > 0)
    .sort((left, right) => {
      const slowDelta = right.slow_turns - left.slow_turns;
      if (slowDelta !== 0) return slowDelta;
      return (right.total_turn_time_ms.p95 ?? 0) - (left.total_turn_time_ms.p95 ?? 0);
    });
  const topProvider = slowProviders[0] ?? null;
  const secondProvider = slowProviders[1] ?? null;
  const totalP95 = data.summary.total_turn_time_ms.p95 ?? null;
  const submitToSendP95 = data.summary.submit_to_send_ms.p95 ?? null;

  if (blockedMachines > 0 || degradedMachines > 0) {
    const machineCount = blockedMachines > 0 ? blockedMachines : degradedMachines;
    const machineLabel =
      blockedMachines > 0
        ? `${machineCount} machine${machineCount === 1 ? "" : "s"} blocked or offline`
        : `${machineCount} machine${machineCount === 1 ? "" : "s"} degraded`;

    cards.push({
      key: "machine",
      eyebrow: "Machine signal",
      title: machineLabel,
      description: unhealthyMachine
        ? `${unhealthyMachine.device_id}: ${unhealthyMachine.status_summary}`
        : "Shipping is not fully healthy on this runtime right now.",
      tone: blockedMachines > 0 ? "error" : "warning",
      to: unhealthyMachine?.device_id
        ? buildTimelineSlicePath({ deviceId: unhealthyMachine.device_id })
        : "/runners",
      ctaLabel: unhealthyMachine?.device_id ? "Open machine sessions" : "Open machines",
    });
  }

  if (data.summary.completed_turns === 0) {
    if (cards.length === 0) {
      cards.push({
        key: "no-turns",
        eyebrow: "Turn signal",
        title: "No completed managed turns in this window",
        description: "This view can still tell you if machine shipping is healthy, but there is no recent turn latency to compare yet.",
        tone: "neutral",
      });
    }
    return cards.slice(0, 3);
  }

  if (topProvider && topProvider.slow_turns > 0) {
    const providerName = toTitleCaseWords(topProvider.provider);
    const dominantBySlowTurns =
      topProvider.slow_turns >= 2 &&
      (
        !secondProvider ||
        secondProvider.slow_turns === 0 ||
        topProvider.slow_turns >= secondProvider.slow_turns * 2
      );
    const dominantByLatency =
      topProvider.completed_turns >= 3 &&
      topProvider.slow_turns >= 2 &&
      !!secondProvider &&
      (topProvider.total_turn_time_ms.p95 ?? 0) >= Math.max(
        data.slow_threshold_ms,
        (secondProvider.total_turn_time_ms.p95 ?? 0) * 1.5,
      );
    const dominant = dominantBySlowTurns || dominantByLatency;

    cards.push({
      key: "provider",
      eyebrow: "Provider signal",
      title: dominant ? `${providerName} is driving most of the slow turns` : "Slow turns span more than one provider",
      description: dominant
        ? `${topProvider.slow_turns} of ${data.summary.slow_turns} slow turns came from ${providerName}. P95 total turn time is ${formatLatencyMs(topProvider.total_turn_time_ms.p95)}.`
        : `${data.summary.slow_turns} slow turns appeared across multiple providers in this window. Start with the slow-turn list, not one machine.`,
      tone: "warning",
      to: buildTimelineSlicePath({ provider: topProvider.provider }),
      ctaLabel: dominant ? `Open ${providerName} sessions` : "Open timeline slice",
    });
  }

  if (submitToSendP95 != null && totalP95 != null && totalP95 > 0) {
    const dispatchShare = submitToSendP95 / totalP95;
    if (submitToSendP95 >= 5_000 || dispatchShare >= 0.35) {
      cards.push({
        key: "dispatch-high",
        eyebrow: "Runtime signal",
        title: "Dispatch overhead is elevated",
        description: `P95 submit→send is ${formatLatencyMs(submitToSendP95)}, about ${Math.round(dispatchShare * 100)}% of the full turn. Longhouse overhead may be part of what users feel.`,
        tone: "warning",
        onAction: callbacks.scrollToSlowTurns,
        ctaLabel: "Open slow turns",
      });
    } else if (data.summary.slow_turns > 0) {
      cards.push({
        key: "dispatch-healthy",
        eyebrow: "Runtime signal",
        title: "Dispatch looks healthy; slowness is later in the turn",
        description: `P95 submit→send is ${formatLatencyMs(submitToSendP95)} against ${formatLatencyMs(totalP95)} total. The slow part is probably after dispatch, not before it.`,
        tone: "success",
      });
    }
  }

  if (cards.length === 0) {
    cards.push({
      key: "healthy",
      eyebrow: "Current window",
      title: "No active health regressions in this slice",
      description: `${data.machine_counts.healthy}/${data.machine_counts.total} machines are healthy and no managed turns crossed ${formatLatencyMs(data.slow_threshold_ms)}.`,
      tone: "success",
    });
  }

  return cards.slice(0, 3);
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

function DiagnosisPanel({ cards }: { cards: DiagnosisCardData[] }) {
  return (
    <Card className="observability-panel observability-panel--diagnosis">
      <Card.Header className="observability-panel__header">
        <div>
          <div className="observability-panel__eyebrow">Health snapshot</div>
          <h3 className="observability-panel__title">What the current window says</h3>
          <p className="observability-panel__description">
            These are the highest-confidence readings from this runtime right now, not a generic chart wall.
          </p>
        </div>
      </Card.Header>
      <Card.Body>
        <div className="observability-diagnosis-list">
          {cards.map((card) => (
            <div
              key={card.key}
              className={`observability-diagnosis-card observability-diagnosis-card--${card.tone}`}
            >
              <div className="observability-diagnosis-card__header">
                <div>
                  <div className="observability-diagnosis-card__eyebrow">{card.eyebrow}</div>
                  <h4 className="observability-diagnosis-card__title">{card.title}</h4>
                </div>
                <Badge variant={diagnosisToneBadgeVariant(card.tone)}>
                  {card.tone === "error"
                    ? "Needs attention"
                    : card.tone === "warning"
                      ? "Watch"
                      : card.tone === "success"
                        ? "Healthy"
                        : "Info"}
                </Badge>
              </div>
              <p className="observability-diagnosis-card__description">{card.description}</p>
              {card.onAction && card.ctaLabel ? (
                <button
                  type="button"
                  onClick={card.onAction}
                  className="ui-button ui-button--ghost ui-button--sm observability-inline-link"
                >
                  {card.ctaLabel}
                </button>
              ) : null}
              {card.to && card.ctaLabel ? (
                <Link to={card.to} className="ui-button ui-button--ghost ui-button--sm observability-inline-link">
                  {card.ctaLabel}
                </Link>
              ) : null}
            </div>
          ))}
        </div>
      </Card.Body>
    </Card>
  );
}

function ProductHealthChecksPanel({
  checks,
  isLoading,
  error,
}: {
  checks: ProductHealthCheckSummaryResponse[];
  isLoading: boolean;
  error: Error | null;
}) {
  return (
    <Card className="observability-panel observability-panel--product-checks">
      <Card.Header className="observability-panel__header">
        <div>
          <div className="observability-panel__eyebrow">Product checks</div>
          <h3 className="observability-panel__title">Can users work right now</h3>
          <p className="observability-panel__description">
            Product-level verdicts derived from persisted runtime observations.
          </p>
        </div>
      </Card.Header>
      <Card.Body>
        {isLoading ? (
          <EmptyState
            icon={<Spinner size="sm" />}
            title="Loading product checks..."
            description="Reading persisted observation evidence."
          />
        ) : error ? (
          <EmptyState
            variant="error"
            title="Product checks unavailable"
            description={error.message}
          />
        ) : checks.length === 0 ? (
          <EmptyState
            title="No product checks available"
            description="Checks appear here once this runtime exposes product health verdicts."
          />
        ) : (
          <div className="observability-product-check-list">
            {checks.map((check) => (
              <div key={check.check} className={`observability-product-check observability-product-check--${check.verdict}`}>
                <div className="observability-product-check__main">
                  <div className="observability-product-check__title-row">
                    <h4 className="observability-product-check__title">{productCheckTitle(check.check)}</h4>
                    <Badge variant={productCheckBadgeVariant(check.verdict)}>{check.verdict}</Badge>
                  </div>
                  <p className="observability-product-check__headline">{check.headline}</p>
                </div>
                <div className="observability-product-check__meta">{productCheckMeta(check)}</div>
              </div>
            ))}
          </div>
        )}
      </Card.Body>
    </Card>
  );
}

function ProviderFocusPanel({ providers }: { providers: ManagedTurnProviderSummaryResponse[] }) {
  if (providers.length === 0) {
    return (
      <EmptyState
        title="No managed turns yet"
        description="Provider slices will show up here once Longhouse has observed completed managed turns."
      />
    );
  }

  return (
    <div className="observability-provider-list">
      {providers.map((provider) => {
        const badge = providerStatusBadge(provider);
        return (
          <Link
            key={provider.provider}
            to={buildTimelineSlicePath({ provider: provider.provider })}
            className="observability-provider-row"
          >
            <div className="observability-provider-row__left">
              <div className="observability-provider-row__name">
                <ProviderGlyph provider={provider.provider} size={20} />
                {toTitleCaseWords(provider.provider)}
              </div>
              <div className="observability-provider-row__meta">
                {provider.completed_turns} turns in this window
              </div>
            </div>
            <div className="observability-provider-row__right">
              <div className="observability-provider-row__stats">
                <span>{formatLatencyMs(provider.total_turn_time_ms.p95)} p95 total</span>
                <span>{formatLatencyMs(provider.submit_to_send_ms.p95)} dispatch</span>
              </div>
              <Badge variant={badge.variant}>{badge.label}</Badge>
            </div>
          </Link>
        );
      })}
    </div>
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
          <HistoryImportPanel historyImport={machine.history_import} />
          <div className="observability-machine-card__actions">
            <Link
              to={buildTimelineSlicePath({ deviceId: machine.device_id })}
              className="ui-button ui-button--ghost ui-button--sm observability-inline-link"
            >
              Open machine sessions
            </Link>
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
        <Table.Cell isHeader>Session</Table.Cell>
        <Table.Cell isHeader>Total</Table.Cell>
        <Table.Cell isHeader>Dispatch</Table.Cell>
        <Table.Cell isHeader>Active→Terminal</Table.Cell>
        <Table.Cell isHeader>Machine</Table.Cell>
      </Table.Header>
      <Table.Body>
        {turns.map((turn) => (
          <Table.Row key={turn.turn_id}>
            <Table.Cell>
              <div className="observability-provider-stack">
                <Link
                  to={buildSessionDetailPath(
                    { id: turn.session_id, provider: turn.provider, match_event_id: null },
                    null,
                  )}
                  className="observability-session-link"
                >
                  {toTitleCaseWords(turn.provider)} · {turn.session_id.slice(0, 8)}
                </Link>
                <span className="observability-cell-subtle">
                  {[turn.project, turn.device_name || turn.device_id || "Machine unknown"].filter(Boolean).join(" · ")}
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
  const slowTurnsRef = useRef<HTMLDivElement | null>(null);
  const { data, isLoading, error } = useQuery({
    queryKey: ["observability-overview", hoursBack],
    queryFn: () => fetchObservabilityOverview(hoursBack),
    enabled: config.singleTenant,
    refetchInterval: 15_000,
    retry: false,
  });
  const {
    data: productHealthData,
    isLoading: productHealthLoading,
    error: productHealthError,
  } = useQuery({
    queryKey: ["observability-product-checks"],
    queryFn: () => fetchProductHealthChecks(),
    enabled: config.singleTenant,
    refetchInterval: 15_000,
    retry: false,
  });

  useReadinessFlag({ ready: !config.singleTenant || !isLoading });

  const scrollToSlowTurns = useCallback(() => {
    slowTurnsRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }, []);
  const diagnosisCards = useMemo(
    () => (data ? buildDiagnosisCards(data, { scrollToSlowTurns }) : []),
    [data, scrollToSlowTurns],
  );

  if (!config.singleTenant) {
    return (
      <PageShell size="wide" className="observability-page-container">
        <EmptyState
          title="Health is single-tenant for now"
          description="This page reads the self-hosted or provisioned runtime health surfaces. Multi-tenant browser access is not wired yet."
        />
      </PageShell>
    );
  }

  if (isLoading) {
    return (
      <PageShell size="wide" className="observability-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading health..."
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
          title="Error loading health"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      </PageShell>
    );
  }

  const unhealthyMachines = data.machine_counts.broken + data.machine_counts.offline + data.machine_counts.degraded;
  const blockedMachines = data.machine_counts.broken + data.machine_counts.offline;
  const visibleSlowTurnRows = data.slow_turns.length;

  return (
    <PageShell size="wide" className="observability-page-container">
      <div className="observability-page">
        <SectionHeader
          title="Health"
          description="Machine shipping and managed-session latency on this runtime. Use this page when a session feels slow or a machine stops shipping."
          actions={
            <div className="observability-controls">
              <span className="observability-controls__label">Window</span>
              <select
                aria-label="Health window"
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
              Slow turns currently mean total turn time of at least {formatLatencyMs(data.slow_threshold_ms)}. This page stays diagnosis-first and links back into real sessions instead of exposing a separate telemetry tool.
            </span>
          </div>

          <ProductHealthChecksPanel
            checks={productHealthData?.checks ?? []}
            isLoading={productHealthLoading}
            error={productHealthError instanceof Error ? productHealthError : null}
          />

          <div className="observability-hero">
            <DiagnosisPanel cards={diagnosisCards} />
            <div className="observability-metrics-grid">
              <MetricCard
                title="Managed Turns"
                value={data.summary.completed_turns}
                subtitle={buildWindowLabel(data.hours_back)}
                accent="var(--color-brand-primary)"
              />
              <MetricCard
                title="Slow Turns"
                value={data.summary.slow_turns}
                subtitle={`Showing ${visibleSlowTurnRows} of ${data.slow_turn_total}`}
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
                subtitle="Longhouse dispatch slice"
                accent="var(--color-intent-success)"
              />
              <MetricCard
                title="Healthy Machines"
                value={`${data.machine_counts.healthy}/${data.machine_counts.total}`}
                subtitle={`${unhealthyMachines} need attention`}
                accent="var(--color-intent-success)"
              />
              <MetricCard
                title="Blocked Machines"
                value={blockedMachines}
                subtitle={`${data.machine_counts.broken} broken, ${data.machine_counts.offline} offline`}
                accent="var(--color-intent-error)"
              />
            </div>
          </div>

          <div className="observability-grid">
            <Card className="observability-panel">
              <Card.Header className="observability-panel__header">
                <div>
                  <div className="observability-panel__eyebrow">Provider focus</div>
                  <h3 className="observability-panel__title">Which providers are contributing to the pain</h3>
                  <p className="observability-panel__description">
                    Keep this as a guided slice, not a metrics explorer. Each row jumps straight into the matching timeline view.
                  </p>
                </div>
              </Card.Header>
              <Card.Body>
                <ProviderFocusPanel providers={data.providers} />
              </Card.Body>
            </Card>

            <Card className="observability-panel">
              <Card.Header className="observability-panel__header">
                <div>
                  <div className="observability-panel__eyebrow">Machine health</div>
                  <h3 className="observability-panel__title">Shipping truth from the latest heartbeats</h3>
                  <p className="observability-panel__description">
                    Use this to tell whether the problem is one unhealthy machine or something broader.
                  </p>
                </div>
                <Link to="/runners" className="ui-button ui-button--ghost ui-button--sm observability-inline-link">
                  Open machines
                </Link>
              </Card.Header>
              <Card.Body>
                <MachineHealthPanel machines={data.machines} />
              </Card.Body>
            </Card>
          </div>

          <div id="health-slow-turns" ref={slowTurnsRef}>
            <Card className="observability-panel">
              <Card.Header className="observability-panel__header">
                <div>
                  <div className="observability-panel__eyebrow">Slow turns</div>
                  <h3 className="observability-panel__title">The slowest managed turns in this window</h3>
                  <p className="observability-panel__description">
                    Each row opens the real session so you can inspect the transcript and timing breakdown in context.
                  </p>
                </div>
                <Link to="/timeline" className="ui-button ui-button--ghost ui-button--sm observability-inline-link">
                  Open timeline
                </Link>
              </Card.Header>
              <Card.Body>
                <SlowTurnsTable turns={data.slow_turns} />
              </Card.Body>
            </Card>
          </div>
        </div>
      </div>
    </PageShell>
  );
}
