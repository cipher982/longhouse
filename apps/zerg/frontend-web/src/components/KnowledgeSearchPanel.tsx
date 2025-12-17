import React, { useState } from "react";
import { useKnowledgeSearch } from "../hooks/useKnowledgeSources";

/**
 * KnowledgeSearchPanel - V1.1 search UI for verifying synced content
 *
 * Users can search their knowledge base to verify documents are
 * properly synced and searchable before using agents.
 */
export function KnowledgeSearchPanel() {
  const [query, setQuery] = useState("");
  const { data: results, isLoading, error } = useKnowledgeSearch(query);

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
            <div key={idx} className="search-result-item" data-testid={`search-result-${idx}`}>
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
                href={result.path}
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
