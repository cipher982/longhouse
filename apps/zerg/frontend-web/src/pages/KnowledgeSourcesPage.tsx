import React, { useState } from "react";
import {
  useKnowledgeSources,
  useDeleteKnowledgeSource,
  useSyncKnowledgeSource,
} from "../hooks/useKnowledgeSources";
import { KnowledgeSourceCard } from "../components/KnowledgeSourceCard";
import { AddKnowledgeSourceModal } from "../components/AddKnowledgeSourceModal";
import { KnowledgeSearchPanel } from "../components/KnowledgeSearchPanel";
import "../styles/knowledge-sources.css";

export default function KnowledgeSourcesPage() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [syncingIds, setSyncingIds] = useState<Set<number>>(new Set());

  const { data: sources, isLoading, error } = useKnowledgeSources();
  const deleteMutation = useDeleteKnowledgeSource();
  const syncMutation = useSyncKnowledgeSource();

  const handleSync = async (id: number) => {
    setSyncingIds((prev) => new Set(prev).add(id));
    try {
      await syncMutation.mutateAsync(id);
    } finally {
      setSyncingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const handleDelete = (id: number) => {
    if (confirm("Are you sure you want to delete this knowledge source?")) {
      deleteMutation.mutate(id);
    }
  };

  if (isLoading) {
    return (
      <div className="profile-container">
        <div className="profile-content">
          <h2>Knowledge Sources</h2>
          <p>Loading...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="profile-container">
        <div className="profile-content">
          <h2>Knowledge Sources</h2>
          <p className="error-message">Failed to load: {String(error)}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="profile-container knowledge-sources-page">
      <div className="profile-content">
        <div className="section-header">
          <div>
            <h2>Knowledge Sources</h2>
            <p className="settings-description">
              Connect knowledge sources to give your agents context about your codebase, documentation, and more.
            </p>
          </div>
          <button className="btn-add" onClick={() => setIsModalOpen(true)} data-testid="add-knowledge-source-btn">
            + Add Source
          </button>
        </div>

        {sources && sources.length === 0 ? (
          <div className="empty-state" data-testid="empty-state">
            <p>No knowledge sources configured yet.</p>
            <p className="empty-state-hint">
              Add a GitHub repository or URL to get started.
            </p>
          </div>
        ) : (
          <>
            <div className="runners-grid">
              {sources?.map((source) => (
                <KnowledgeSourceCard
                  key={source.id}
                  source={source}
                  onSync={handleSync}
                  onDelete={handleDelete}
                  isSyncing={syncingIds.has(source.id)}
                />
              ))}
            </div>
            {/* V1.1: Search panel to verify synced content */}
            <KnowledgeSearchPanel />
          </>
        )}
      </div>

      <AddKnowledgeSourceModal
        isOpen={isModalOpen}
        onClose={() => setIsModalOpen(false)}
      />
    </div>
  );
}
