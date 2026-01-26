import React, { useState, useEffect } from "react";
import { useKnowledgeSearch } from "../hooks/useKnowledgeSources";

/**
 * Custom hook for debouncing a value with a timed delay.
 */
function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedValue(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return debouncedValue;
}

/**
 * KnowledgeSearchPanel - V1.1 search UI for verifying synced content
 *
 * Users can search their knowledge base to verify documents are
 * properly synced and searchable before using fiches.
 */
export function KnowledgeSearchPanel() {
  const [query, setQuery] = useState("");
  // V1.1: Debounce search queries to throttle API requests (300ms delay)
  const debouncedQuery = useDebounce(query, 300);
  const { data: results, isLoading, error } = useKnowledgeSearch(debouncedQuery);

  return (
    <div className="knowledge-search-panel" data-testid="knowledge-search-panel">
      <div className="search-panel-header">
        <h3>Search Knowledge Base</h3>
        <p className="search-panel-hint">
          Test search to verify your synced content is discoverable
        </p>
      </div>

      <div className="search-input-container">
        <input
          type="text"
          className="search-input"
          placeholder="Search your synced content..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="knowledge-search-input"
        />
      </div>

      {query.length > 0 && query.length < 2 && (
        <p className="search-hint">Type at least 2 characters to search</p>
      )}

      {isLoading && <p className="loading-text">Searching...</p>}

      {error && (
        <p className="error-message">
          Search failed: {String(error)}
        </p>
      )}

      {results && results.length === 0 && query.length >= 2 && (
        <p className="search-no-results">
          No results found for &quot;{query}&quot;
        </p>
      )}

      {results && results.length > 0 && (
        <div className="search-results" data-testid="knowledge-search-results">
          {results.map((result, idx) => (
            // V1.1: Use document_id as key for stable React reconciliation
            <div key={result.document_id} className="search-result-item" data-testid={`search-result-${idx}`}>
              <div className="search-result-header">
                <span className="search-result-icon">📄</span>
                <span className="search-result-title">{result.title}</span>
              </div>
              <p className="search-result-source">
                Source: {result.source_name}
              </p>
              {result.snippets && result.snippets.length > 0 && (
                <p className="search-result-snippet">
                  {result.snippets[0]}
                </p>
              )}
              <a
                // V1.1: Prefer permalink (immutable) over path (branch URL)
                href={result.permalink || result.path}
                target="_blank"
                rel="noopener noreferrer"
                className="search-result-link"
              >
                View source
              </a>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
