/**
 * BriefingsPage — Project briefings from recent sessions.
 *
 * Compiles recent session summaries, insights, and action proposals
 * into a readable briefing for a given project.
 */

import { useState, useCallback } from "react";
import { useBriefing } from "../hooks/useAgentSessions";
import { useAgentFilters } from "../hooks/useAgentSessions";
import {
  PageShell,
  SectionHeader,
  EmptyState,
  Spinner,
  Button,
  Badge,
} from "../components/ui";

export function BriefingsPage() {
  const [project, setProject] = useState("");
  const [sessionLimit, setSessionLimit] = useState(5);

  const { data: filtersData } = useAgentFilters(90);
  const projectOptions = filtersData?.projects ?? [];

  const { data, isLoading, error, refetch, isFetching } = useBriefing(project, sessionLimit);

  const handleCopy = useCallback(() => {
    if (data?.briefing) {
      navigator.clipboard.writeText(data.briefing).catch(() => {});
    }
  }, [data?.briefing]);

  return (
    <PageShell size="normal">
      <SectionHeader
        title="Briefings"
        description="A summary of recent sessions, insights, and action items for a project."
        actions={
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            {data?.briefing && (
              <Button variant="ghost" size="sm" onClick={handleCopy}>
                Copy
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => refetch()}
              disabled={isFetching || !project}
              aria-label="Refresh briefing"
            >
              {isFetching ? <Spinner size="sm" /> : "Refresh"}
            </Button>
          </div>
        }
      />

      {/* Controls */}
      <div className="briefings-controls" data-testid="briefings-controls">
        <div className="briefings-project-select">
          <label htmlFor="briefings-project" className="briefings-label">
            Project
          </label>
          <select
            id="briefings-project"
            className="briefings-select"
            value={project}
            onChange={(e) => setProject(e.target.value)}
            data-testid="briefings-project-select"
          >
            <option value="">Select a project…</option>
            {projectOptions.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>

        <div className="briefings-limit-select">
          <label htmlFor="briefings-limit" className="briefings-label">
            Sessions
          </label>
          <select
            id="briefings-limit"
            className="briefings-select briefings-select--narrow"
            value={sessionLimit}
            onChange={(e) => setSessionLimit(Number(e.target.value))}
          >
            <option value={3}>3</option>
            <option value={5}>5</option>
            <option value={10}>10</option>
            <option value={20}>20</option>
          </select>
        </div>
      </div>

      {/* Body */}
      <div className="briefings-body">
        {!project && (
          <EmptyState
            title="Select a project"
            description="Choose a project above to generate a briefing from recent sessions."
          />
        )}

        {project && isLoading && (
          <div className="briefings-loading">
            <Spinner size="md" />
            <span>Generating briefing…</span>
          </div>
        )}

        {project && error && (
          <EmptyState
            variant="error"
            title="Briefing unavailable"
            description="Embeddings or summaries may not be configured for this instance."
          />
        )}

        {project && !isLoading && data && !data.briefing && (
          <EmptyState
            title="No briefing available"
            description={`No recent sessions found for "${project}", or summaries have not been generated yet.`}
          />
        )}

        {data?.briefing && (
          <div className="briefings-result" data-testid="briefings-result">
            <div className="briefings-result-meta">
              <Badge variant="neutral">{data.session_count} sessions</Badge>
              <span className="briefings-result-project">{data.project}</span>
            </div>
            <pre className="briefings-text">{data.briefing}</pre>
          </div>
        )}
      </div>
    </PageShell>
  );
}
