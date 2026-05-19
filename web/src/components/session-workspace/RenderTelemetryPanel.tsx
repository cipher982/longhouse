import { useQuery } from "@tanstack/react-query";
import { Badge, Button, Spinner } from "../ui";
import { parseUTC } from "../../lib/dateUtils";
import {
  fetchRecentClientRenderBeacons,
  type ClientRenderBeaconItem,
} from "../../services/api/telemetry";

interface RenderTelemetryPanelProps {
  sessionId: string;
}

function formatMetric(value: number | null | undefined, suffix: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "n/a";
  }
  return `${Math.round(value)}${suffix}`;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "n/a";
  const parsed = parseUTC(value);
  if (Number.isNaN(parsed.getTime())) return "n/a";
  return parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatEventId(value: string | null | undefined): string {
  if (!value) return "unknown event";
  return `event ${value}`;
}

function surfaceVariant(surface: string | null | undefined): "neutral" | "success" | "warning" {
  if (surface === "ios") return "success";
  if (surface === "web") return "neutral";
  return "warning";
}

function RenderTelemetryRow({ beacon }: { beacon: ClientRenderBeaconItem }) {
  const surface = beacon.surface || "unknown";
  return (
    <li className="render-telemetry-panel__row">
      <div className="render-telemetry-panel__row-header">
        <span className="render-telemetry-panel__event">
          {formatEventId(beacon.event_id)}
        </span>
        <Badge variant={surfaceVariant(beacon.surface)}>{surface}</Badge>
      </div>
      <dl className="render-telemetry-panel__metrics">
        <div>
          <dt>latency</dt>
          <dd>{formatMetric(beacon.latency_ms, "ms")}</dd>
        </div>
        <div>
          <dt>clock skew</dt>
          <dd>{formatMetric(beacon.clock_skew_ms, "ms")}</dd>
        </div>
        <div>
          <dt>observed</dt>
          <dd>{formatTimestamp(beacon.observed_at)}</dd>
        </div>
        <div>
          <dt>received</dt>
          <dd>{formatTimestamp(beacon.received_at)}</dd>
        </div>
      </dl>
    </li>
  );
}

export function RenderTelemetryPanel({ sessionId }: RenderTelemetryPanelProps) {
  const query = useQuery({
    queryKey: ["client-render-beacons", sessionId],
    queryFn: () => fetchRecentClientRenderBeacons({ sessionId, limit: 8 }),
    enabled: false,
    retry: false,
  });
  const items = query.data?.items ?? [];
  const hasLoaded = query.data !== undefined;

  return (
    <aside className="event-inspector render-telemetry-panel" data-testid="render-telemetry-panel">
      <div className="event-inspector__header">
        <div>
          <div className="event-inspector__title">Render telemetry</div>
          <div className="event-inspector__subtitle">
            Recent transcript render beacons for this session.
          </div>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          {query.isFetching ? <Spinner size="sm" /> : null}
          {hasLoaded ? "Refresh" : "Load"}
        </Button>
      </div>
      <div className="event-inspector__body">
        {query.isError ? (
          <div className="inspector-empty-block" role="alert">
            Render telemetry unavailable.
          </div>
        ) : null}
        {!query.isError && !hasLoaded ? (
          <div className="inspector-empty-block">
            Load recent render beacons to compare web and iOS transcript delivery.
          </div>
        ) : null}
        {!query.isError && hasLoaded && items.length === 0 ? (
          <div className="inspector-empty-block">No render beacons recorded yet.</div>
        ) : null}
        {items.length > 0 ? (
          <ol className="render-telemetry-panel__list">
            {items.map((beacon, index) => (
              <RenderTelemetryRow
                key={`${beacon.surface ?? "unknown"}:${beacon.event_id ?? "event"}:${beacon.received_at ?? index}`}
                beacon={beacon}
              />
            ))}
          </ol>
        ) : null}
      </div>
    </aside>
  );
}
