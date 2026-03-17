import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAgentFilters } from "../hooks/useAgentSessions";
import {
  useArchiveInsight,
  useInsights,
  useUnarchiveInsight,
} from "../hooks/useInsights";
import { parseUTC } from "../lib/dateUtils";
import {
  Badge,
  Button,
  EmptyState,
  PageShell,
  SectionHeader,
  Spinner,
  Table,
} from "../components/ui";

const INSIGHT_TYPES = [
  "all",
  "learning",
  "pattern",
  "failure",
  "improvement",
] as const;
const ORIGIN_FILTERS = ["all", "manual", "reflection", "system"] as const;
const ARCHIVED_FILTERS = ["active", "all", "archived"] as const;
const LOOKBACK_HOURS = 24 * 365;

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  return parseUTC(iso).toLocaleDateString();
}

function originLabel(origin: string | null): string {
  return origin ?? "legacy";
}

function severityVariant(severity: string): "neutral" | "warning" | "error" {
  if (severity === "critical") return "error";
  if (severity === "warning") return "warning";
  return "neutral";
}

export function InsightsPage() {
  const [searchParams] = useSearchParams();
  const [project, setProject] = useState(searchParams.get("project") ?? "");
  const [insightType, setInsightType] =
    useState<(typeof INSIGHT_TYPES)[number]>("all");
  const [originFilter, setOriginFilter] =
    useState<(typeof ORIGIN_FILTERS)[number]>("all");
  const [archivedFilter, setArchivedFilter] =
    useState<(typeof ARCHIVED_FILTERS)[number]>("active");

  const { data: filtersData } = useAgentFilters(365);
  const projectOptions = useMemo(() => {
    const options = new Set(filtersData?.projects ?? []);
    if (project) options.add(project);
    return [...options].sort();
  }, [filtersData?.projects, project]);

  const includeArchived = archivedFilter !== "active";
  const includeSystem = originFilter === "all" || originFilter === "system";

  const { data, isLoading, error, refetch, isFetching } = useInsights({
    project: project || undefined,
    insight_type: insightType === "all" ? undefined : insightType,
    since_hours: LOOKBACK_HOURS,
    limit: 100,
    include_archived: includeArchived,
    include_system: includeSystem,
  });
  const archive = useArchiveInsight();
  const unarchive = useUnarchiveInsight();

  const visibleInsights = useMemo(() => {
    const rows = data?.insights ?? [];
    return rows.filter((row) => {
      if (originFilter !== "all" && originLabel(row.origin) !== originFilter) {
        return false;
      }
      if (archivedFilter === "archived" && !row.archived_at) {
        return false;
      }
      return !(archivedFilter === "active" && row.archived_at);
    });
  }, [archivedFilter, data?.insights, originFilter]);

  return (
    <PageShell size="wide">
      <SectionHeader
        title="Insights"
        description="Curate the continuity-memory corpus used by briefings and machine reads."
        actions={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            {isFetching ? "Refreshing..." : "Refresh"}
          </Button>
        }
      />

      <div
        style={{
          display: "grid",
          gap: "0.75rem",
          gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
          marginBottom: "1rem",
        }}
      >
        <label style={{ display: "grid", gap: "0.35rem" }}>
          <span>Project</span>
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">All projects</option>
            {projectOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "grid", gap: "0.35rem" }}>
          <span>Type</span>
          <select
            value={insightType}
            onChange={(e) =>
              setInsightType(e.target.value as (typeof INSIGHT_TYPES)[number])
            }
          >
            {INSIGHT_TYPES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "grid", gap: "0.35rem" }}>
          <span>Origin</span>
          <select
            value={originFilter}
            onChange={(e) =>
              setOriginFilter(e.target.value as (typeof ORIGIN_FILTERS)[number])
            }
          >
            {ORIGIN_FILTERS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "grid", gap: "0.35rem" }}>
          <span>State</span>
          <select
            value={archivedFilter}
            onChange={(e) =>
              setArchivedFilter(
                e.target.value as (typeof ARCHIVED_FILTERS)[number],
              )
            }
          >
            {ARCHIVED_FILTERS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error ? (
        <EmptyState
          variant="error"
          title="Failed to load insights"
          description={error.message}
          action={
            <Button variant="primary" onClick={() => refetch()}>
              Try Again
            </Button>
          }
        />
      ) : isLoading ? (
        <EmptyState icon={<Spinner size="lg" />} title="Loading insights..." />
      ) : visibleInsights.length === 0 ? (
        <EmptyState
          title="No insights"
          description="No insights match the current filters."
        />
      ) : (
        <Table>
          <Table.Header>
            <Table.Cell isHeader>Insight</Table.Cell>
            <Table.Cell isHeader>Project</Table.Cell>
            <Table.Cell isHeader>Type</Table.Cell>
            <Table.Cell isHeader>Origin</Table.Cell>
            <Table.Cell isHeader>Created</Table.Cell>
            <Table.Cell isHeader>Status</Table.Cell>
            <Table.Cell isHeader>Action</Table.Cell>
          </Table.Header>
          <Table.Body>
            {visibleInsights.map((insight) => {
              const isArchiving =
                archive.isPending && archive.variables === insight.id;
              const isRestoring =
                unarchive.isPending && unarchive.variables === insight.id;
              return (
                <Table.Row key={insight.id}>
                  <Table.Cell>
                    <div style={{ display: "grid", gap: "0.35rem" }}>
                      <strong>{insight.title}</strong>
                      {insight.description && (
                        <span
                          style={{
                            color: "var(--text-secondary)",
                            fontSize: "0.875rem",
                          }}
                        >
                          {insight.description}
                        </span>
                      )}
                    </div>
                  </Table.Cell>
                  <Table.Cell>{insight.project ?? "global"}</Table.Cell>
                  <Table.Cell>
                    <Badge variant={severityVariant(insight.severity)}>
                      {insight.insight_type}
                    </Badge>
                  </Table.Cell>
                  <Table.Cell>{originLabel(insight.origin)}</Table.Cell>
                  <Table.Cell>{formatDate(insight.created_at)}</Table.Cell>
                  <Table.Cell>
                    {insight.archived_at
                      ? `Archived ${formatDate(insight.archived_at)}`
                      : "Active"}
                  </Table.Cell>
                  <Table.Cell>
                    {insight.archived_at ? (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => unarchive.mutate(insight.id)}
                        disabled={isRestoring || isArchiving}
                      >
                        {isRestoring ? "Restoring..." : "Restore"}
                      </Button>
                    ) : (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => archive.mutate(insight.id)}
                        disabled={isArchiving || isRestoring}
                      >
                        {isArchiving ? "Archiving..." : "Archive"}
                      </Button>
                    )}
                  </Table.Cell>
                </Table.Row>
              );
            })}
          </Table.Body>
        </Table>
      )}
    </PageShell>
  );
}
