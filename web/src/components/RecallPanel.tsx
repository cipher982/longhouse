/**
 * RecallPanel — Turn-level semantic knowledge retrieval.
 *
 * Returns small result cards first and fetches transcript context only when a
 * person opens one. Results link directly to the full session in Timeline.
 */

import { useState } from "react";
import "../styles/recall-panel.css";
import { Link } from "react-router-dom";
import { useRecall, useRecallContext } from "../hooks/useAgentSessions";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import type { RecallSearchResult, RecallExpandedTurn, RecallFilters } from "../services/api/agents";
import { Badge, Input, Spinner, EmptyState } from "./ui";

interface RecallPanelProps {
  /** Pre-filter results to a specific project */
  project?: string;
  /** Pre-filter results to a specific provider */
  provider?: string;
}

function ContextTurn({ turn }: { turn: RecallExpandedTurn }) {
  // Neutral assistant label: recall spans Claude, Codex, Antigravity, OpenCode,
  // Provider-specific launch hints live in sessionWorkspace/interaction.ts,
  // so don't hardcode a single provider name.
  const roleLabel = turn.role === "user" ? "User" : turn.tool_name ? turn.tool_name : "Assistant";
  const roleClass = turn.role === "user" ? "recall-turn--user" : "recall-turn--assistant";
  const matchClass = turn.is_match ? "recall-turn--match" : "";

  return (
    <div className={`recall-turn ${roleClass} ${matchClass}`.trim()}>
      <span className="recall-turn-role">{roleLabel}</span>
      <span className="recall-turn-content">{turn.content_text}</span>
    </div>
  );
}

function retrievalProvenance(result: RecallSearchResult): string {
  return result.matched_by.map((lane) => lane === "dense" ? "Semantic" : "Lexical").join(" + ");
}

function RecallCard({ result }: { result: RecallSearchResult }) {
  const [expanded, setExpanded] = useState(false);
  const context = useRecallContext(expanded ? result.ref : null);
  const sessionLink = `/timeline/${result.session_id}`;
  const sourceTitle = [result.project || "Unknown project", result.provider].filter(Boolean).join(" · ");
  const matchedTurn = result.matched_tool_name || result.matched_role;
  const sourceLine = [
    result.started_at,
    matchedTurn,
    `${result.total_events} event${result.total_events === 1 ? "" : "s"}`,
    `Session ${result.session_id.slice(0, 8)}…`,
  ].filter(Boolean).join(" · ");

  return (
    <div
      className="recall-card"
      data-testid="recall-card"
    >
      <div className="recall-card-header">
        <Link
          to={sessionLink}
          className="recall-card-session-link"
          title="Open session"
          {...{ elementtiming: "longhouse-recall-card" }}
        >
          {sourceTitle}
        </Link>
        <Badge variant="neutral">{retrievalProvenance(result)}</Badge>
      </div>
      <div className="recall-card-meta">{sourceLine}</div>
      <div className="recall-card-context">
        {result.snippet ? (
          <div className="recall-card-evidence">{result.snippet}</div>
        ) : (
          <div className="recall-card-evidence recall-card-evidence--unavailable">
            Snippet unavailable: {result.snippet_unavailable_reason || "unknown"}
          </div>
        )}
        {expanded && context.isLoading && (
          <div className="recall-loading"><Spinner size="sm" /><span>Opening result…</span></div>
        )}
        {expanded && context.error && (
          <div className="recall-card-evidence recall-card-evidence--unavailable">Context unavailable.</div>
        )}
        {expanded && context.data?.turns.map((turn, index) => (
          <ContextTurn key={`${result.ref}-${index}`} turn={turn} />
        ))}
      </div>
      <div className="recall-card-actions">
        <button
          type="button"
          className="recall-card-open"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Hide context" : "Show context"}
        </button>
        <Link
          to={sessionLink}
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
  };

  const { data, isLoading, error } = useRecall(filters);
  const matches = data?.results ?? [];
  const total = data?.total ?? 0;
  const coverageSummary = data?.coverage
    ? data.coverage.complete
      ? "Corpus current"
      : `Corpus snapshot · ${data.coverage.lagging_sessions} session${data.coverage.lagging_sessions === 1 ? "" : "s"} updating`
    : null;

  return (
    <div className="recall-panel" data-testid="recall-panel">
      <div className="recall-panel-header">
        <h3 className="recall-panel-title">Recall</h3>
        <p className="recall-panel-description">Search compact conversation snippets, then open only the useful results.</p>
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
              {coverageSummary && ` · ${coverageSummary}`}
            </div>
            <div className="recall-results-list" data-testid="recall-results">
              {matches.map((match) => (
                <RecallCard key={match.ref} result={match} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
