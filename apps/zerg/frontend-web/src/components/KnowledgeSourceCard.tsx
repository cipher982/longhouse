import React from "react";
import type { KnowledgeSource } from "../services/api";

interface KnowledgeSourceCardProps {
  source: KnowledgeSource;
  onSync: (id: number) => void;
  onDelete: (id: number) => void;
  isSyncing?: boolean;
}

export function KnowledgeSourceCard({
  source,
  onSync,
  onDelete,
  isSyncing,
}: KnowledgeSourceCardProps) {
  const getStatusBadgeClass = (status: string) => {
    switch (status) {
      case "success":
        return "status-badge status-online";
      case "syncing":
        return "status-badge status-syncing";
      case "failed":
        return "status-badge status-offline";
      default:
        return "status-badge status-pending";
    }
  };

  const getStatusLabel = (status: string) => {
    switch (status) {
      case "success":
        return "Synced";
      case "syncing":
        return "Syncing...";
      case "failed":
        return "Failed";
      default:
        return "Pending";
    }
  };

  const getSourceTypeIcon = (type: string) => {
    switch (type) {
      case "github_repo":
        return "github";
      case "url":
        return "link";
      default:
        return "file";
    }
  };

  const getSourceDescription = () => {
    if (source.source_type === "github_repo") {
      const config = source.config as { owner?: string; repo?: string; branch?: string };
      return `${config.owner}/${config.repo}${config.branch ? ` (${config.branch})` : ""}`;
    } else if (source.source_type === "url") {
      const config = source.config as { url?: string };
      return config.url || "URL source";
    }
    return source.source_type;
  };

  const formatDate = (dateString: string | null | undefined) => {
    if (!dateString) return "Never";
    const date = new Date(dateString);
    return date.toLocaleDateString() + " " + date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };

  return (
    <div className="runner-card knowledge-source-card" data-testid={`knowledge-source-${source.id}`}>
      <div className="runner-card-header">
        <div className="source-header-left">
          <span className={`source-icon source-icon-${getSourceTypeIcon(source.source_type)}`}>
            {source.source_type === "github_repo" ? (
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
              </svg>
            )}
          </span>
          <h3>{source.name}</h3>
        </div>
        <span className={getStatusBadgeClass(source.sync_status)}>
          {getStatusLabel(source.sync_status)}
        </span>
      </div>

      <div className="runner-card-details">
        <div className="runner-detail-row">
          <span className="runner-detail-label">Type:</span>
          <span className="runner-detail-value">
            {source.source_type === "github_repo" ? "GitHub Repository" : "URL"}
          </span>
        </div>
        <div className="runner-detail-row">
          <span className="runner-detail-label">Source:</span>
          <span className="runner-detail-value source-description">
            {getSourceDescription()}
          </span>
        </div>
        <div className="runner-detail-row">
          <span className="runner-detail-label">Last Synced:</span>
          <span className="runner-detail-value">{formatDate(source.last_synced_at)}</span>
        </div>
        {source.sync_error && (
          <div className="runner-detail-row">
            <span className="runner-detail-label">Error:</span>
            <span className="runner-detail-value source-error">{source.sync_error}</span>
          </div>
        )}
      </div>

      <div className="runner-card-actions">
        <button
          className="runner-action-button"
          onClick={() => onSync(source.id)}
          disabled={isSyncing || source.sync_status === "syncing"}
          data-testid={`sync-source-${source.id}`}
        >
          {isSyncing || source.sync_status === "syncing" ? "Syncing..." : "Sync Now"}
        </button>
        <button
          className="runner-action-button runner-action-button-danger"
          onClick={() => onDelete(source.id)}
        >
          Delete
        </button>
      </div>
    </div>
  );
}
