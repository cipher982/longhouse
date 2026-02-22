/**
 * RecallPanel — Turn-level semantic knowledge retrieval.
 *
 * Searches conversation turn embeddings and returns matched turns with
 * surrounding context. Results link directly to the session in the Timeline.
 */

import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { useRecall } from "../hooks/useAgentSessions";
import type { RecallMatch, RecallContextTurn, RecallFilters } from "../services/api/agents";
import { Badge, Input, Spinner, EmptyState } from "./ui";

interface RecallPanelProps {
  /** Pre-filter results to a specific project */
  project?: string;
}

function ContextTurn({ turn }: { turn: RecallContextTurn }) {
  const roleLabel = turn.role === "user" ? "User" : turn.tool_name ? turn.tool_name : "Claude";
  const roleClass = turn.role === "user" ? "recall-turn--user" : "recall-turn--assistant";
  const matchClass = turn.is_match ? "recall-turn--match" : "";

  return (
    <div className={`recall-turn ${roleClass} ${matchClass}`.trim()}>
      <span className="recall-turn-role">{roleLabel}</span>
      <span className="recall-turn-content">{turn.content}</span>
    </div>
  );
}

function RecallCard({ match }: { match: RecallMatch }) {
  const scorePercent = Math.round(match.score * 100);

  return (
    <div className="recall-card" data-testid="recall-card">
      <div className="recall-card-header">
        <Link
          to={`/timeline/${match.session_id}`}
          className="recall-card-session-link"
          title="Open session"
        >
          Session {match.session_id.slice(0, 8)}…
        </Link>
        <Badge variant="neutral">
          {scorePercent}%
        </Badge>
        <span className="recall-card-meta">
          {match.total_events} events
        </span>
      </div>
      <div className="recall-card-context">
        {match.context.map((turn) => (
          <ContextTurn key={turn.index} turn={turn} />
        ))}
      </div>
      <div className="recall-card-actions">
        <Link to={`/timeline/${match.session_id}`} className="recall-card-open">
          Open session →
        </Link>
      </div>
    </div>
  );
}

export function RecallPanel({ project }: RecallPanelProps) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  // Debounce
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 400);
    return () => clearTimeout(t);
  }, [query]);

  const filters: RecallFilters = {
    query: debouncedQuery,
    project: project || undefined,
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
        <p className="recall-panel-description">
          Search conversation turns by meaning — returns exact snippets with context.
        </p>
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
            description="Embeddings may not be configured for this instance."
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
