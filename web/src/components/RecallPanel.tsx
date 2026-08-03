/**
 * RecallPanel — Turn-level semantic knowledge retrieval.
 *
 * Searches conversation turn embeddings and returns matched turns with
 * surrounding context. Results link directly to the session in the Timeline.
 */

import { useState } from "react";
import "../styles/recall-panel.css";
import { Link } from "react-router-dom";
import { useRecall } from "../hooks/useAgentSessions";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import type { RecallMatch, RecallContextTurn, RecallFilters } from "../services/api/agents";
import { Badge, Input, Spinner, EmptyState } from "./ui";

interface RecallPanelProps {
  /** Pre-filter results to a specific project */
  project?: string;
  /** Pre-filter results to a specific provider */
  provider?: string;
}

function ContextTurn({ turn, isMatch }: { turn: RecallContextTurn; isMatch: boolean }) {
  // Neutral assistant label: recall spans Claude, Codex, Antigravity, OpenCode,
  // Provider-specific launch hints live in sessionWorkspace/interaction.ts,
  // so don't hardcode a single provider name.
  const roleLabel = turn.role === "user" ? "User" : turn.tool_name ? turn.tool_name : "Assistant";
  const roleClass = turn.role === "user" ? "recall-turn--user" : "recall-turn--assistant";
  const matchClass = isMatch ? "recall-turn--match" : "";

  return (
    <div className={`recall-turn ${roleClass} ${matchClass}`.trim()}>
      <span className="recall-turn-role">{roleLabel}</span>
      <span className="recall-turn-content">{turn.content_text}</span>
    </div>
  );
}

function retrievalProvenance(match: RecallMatch): string {
  if (!match.retrieval_lanes?.length) return "Source unavailable";
  return [...match.retrieval_lanes]
    .sort((left, right) => (match.lane_ranks?.[left] ?? Number.MAX_SAFE_INTEGER) - (match.lane_ranks?.[right] ?? Number.MAX_SAFE_INTEGER))
    .map((lane) => {
      const label = lane === "dense" ? "Semantic" : "Lexical";
      const rank = match.lane_ranks?.[lane];
      return rank ? `${label} #${rank}` : label;
    })
    .join(" + ");
}

function RecallCard({ match }: { match: RecallMatch }) {
  const eventLink = match.match_event_id != null
    ? `/timeline/${match.session_id}?event_id=${match.match_event_id}`
    : `/timeline/${match.session_id}`;
  const evidenceLabel = match.evidence_status === "partial"
    ? "Partial evidence"
    : match.evidence_status === "unavailable"
      ? "Evidence unavailable"
      : null;

  return (
    <div
      className="recall-card"
      data-testid="recall-card"
    >
      <div className="recall-card-header">
        <Link
          to={eventLink}
          className="recall-card-session-link"
          title="Open session"
          {...{ elementtiming: "longhouse-recall-card" }}
        >
          Session {match.session_id.slice(0, 8)}…
        </Link>
        <Badge variant="neutral">{retrievalProvenance(match)}</Badge>
        {evidenceLabel && (
          <Badge variant={match.evidence_status === "unavailable" ? "error" : "warning"}>
            {evidenceLabel}
          </Badge>
        )}
        <span className="recall-card-meta">
          {match.total_events} events
        </span>
      </div>
      <div className="recall-card-context">
        {match.context.length > 0 ? (
          match.context.map((turn) => (
            <ContextTurn
              key={turn.search_event_id}
              turn={turn}
              isMatch={
                turn.search_event_id === match.match_event_id
                || (!!match.evidence && turn.content_text === match.evidence)
              }
            />
          ))
        ) : match.evidence ? (
          <div className="recall-card-evidence">{match.evidence}</div>
        ) : (
          <div className="recall-card-evidence recall-card-evidence--unavailable">
            {match.evidence_reason || "No evidence was returned for this match."}
          </div>
        )}
      </div>
      <div className="recall-card-actions">
        <Link
          to={eventLink}
          className="recall-card-open"
        >
          Open session →
        </Link>
      </div>
    </div>
  );
}

export function RecallPanel({ project, provider }: RecallPanelProps) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<"auto" | "lexical" | "semantic">("auto");
  const debouncedQuery = useDebouncedValue(query, 400);

  const filters: RecallFilters = {
    query: debouncedQuery,
    project: project || undefined,
    provider: provider || undefined,
    mode,
    since_days: 90,
    max_results: 8,
    context_turns: 2,
  };

  const { data, isLoading, error } = useRecall(filters);
  const matches = data?.matches ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="recall-panel" data-testid="recall-panel">
      <div className="recall-panel-header">
        <h3 className="recall-panel-title">Recall</h3>
        <p className="recall-panel-description">Search conversation turns with explicit retrieval lanes and context.</p>
      </div>

      <div className="recall-panel-search">
        <Input
          type="search"
          placeholder="What did we discuss about authentication?"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="recall-search-input"
          data-testid="recall-search-input"
          aria-label="Recall search query"
        />
        <div className="recall-mode" role="group" aria-label="Recall retrieval mode">
          {(["auto", "semantic", "lexical"] as const).map((option) => (
            <button
              key={option}
              type="button"
              className={`recall-mode-option${mode === option ? " is-active" : ""}`}
              aria-pressed={mode === option}
              onClick={() => setMode(option)}
            >
              {option === "auto" ? "Hybrid" : option === "semantic" ? "Semantic" : "Lexical"}
            </button>
          ))}
        </div>
      </div>

      <div className="recall-panel-results">
        {isLoading && (
          <div className="recall-loading">
            <Spinner size="sm" />
            <span>Searching conversation history…</span>
          </div>
        )}

        {error && (
          <EmptyState
            variant="error"
            title="Recall unavailable"
            description="The recall index is not ready. Try again after indexing finishes."
          />
        )}

        {!isLoading && !error && debouncedQuery && matches.length === 0 && (
          <EmptyState
            title="No matches found"
            description={`No conversation turns matched "${debouncedQuery}".`}
          />
        )}

        {!isLoading && !error && !debouncedQuery && (
          <EmptyState
            title="Search your sessions"
            description="Type a query to find relevant conversation turns across all your sessions."
          />
        )}

        {!isLoading && matches.length > 0 && (
          <>
            <div className="recall-results-header" role="status">
              {total} match{total !== 1 ? "es" : ""}
              {project && ` in ${project}`}
              {data?.coverage && ` · Complete corpus: ${data.coverage.expected_episodes.toLocaleString()} episodes`}
            </div>
            <div className="recall-results-list" data-testid="recall-results">
              {matches.map((match) => (
                <RecallCard key={`${match.session_id}-${match.chunk_index}`} match={match} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
