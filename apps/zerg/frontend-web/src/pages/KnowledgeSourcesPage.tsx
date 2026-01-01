import React, { useState } from "react";
import {
  useKnowledgeSources,
  useDeleteKnowledgeSource,
  useSyncKnowledgeSource,
} from "../hooks/useKnowledgeSources";
import { KnowledgeSourceCard } from "../components/KnowledgeSourceCard";
import { AddKnowledgeSourceModal } from "../components/AddKnowledgeSourceModal";
import { AddContextModal } from "../components/AddContextModal";
import { KnowledgeSearchPanel } from "../components/KnowledgeSearchPanel";
import {
  Button,
  SectionHeader,
  EmptyState
} from "../components/ui";
import { PlusIcon } from "../components/icons";
import "../styles/knowledge-sources.css";
import "../components/AddContextModal.css";

export default function KnowledgeSourcesPage() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isContextModalOpen, setIsContextModalOpen] = useState(false);
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

  // Phase 1: Mock handler - will wire up API in Phase 2
  const handleContextSubmit = async (title: string, content: string) => {
    console.log("AddContextModal submit:", { title, content });
    // TODO: Call API to create knowledge source with source_type: "user_text"
    await new Promise((resolve) => setTimeout(resolve, 500)); // Simulate API delay
  };

  if (isLoading) {
    return (
      <div className="knowledge-sources-page-container">
        <EmptyState
          icon={<div className="spinner" style={{ width: 40, height: 40 }} />}
          title="Loading knowledge sources..."
          description="Fetching your connected documentation and codebases."
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="knowledge-sources-page-container">
        <EmptyState
          variant="error"
          title="Error loading knowledge sources"
          description={String(error)}
        />
      </div>
    );
  }

  return (
    <div className="knowledge-sources-page-container">
      <SectionHeader
        title="Knowledge Sources"
        description="Connect knowledge sources to give your agents context about your codebase, documentation, and more."
        actions={
          <div style={{ display: "flex", gap: "0.5rem" }}>
            <Button variant="secondary" onClick={() => setIsContextModalOpen(true)} data-testid="add-context-btn">
              <PlusIcon /> Add Context
            </Button>
            <Button variant="primary" onClick={() => setIsModalOpen(true)} data-testid="add-knowledge-source-btn">
              <PlusIcon /> Add Source
            </Button>
          </div>
        }
      />

      <div className="knowledge-sources-content">
        {sources && sources.length === 0 ? (
          <EmptyState
            title="No knowledge sources configured yet"
            description="Add a GitHub repository or URL to get started."
            action={
              <Button variant="primary" onClick={() => setIsModalOpen(true)}>
                Add your first source
              </Button>
            }
          />
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

      <AddContextModal
        isOpen={isContextModalOpen}
        onClose={() => setIsContextModalOpen(false)}
        onSubmit={handleContextSubmit}
        existingDocsCount={sources?.length ?? 0}
      />
    </div>
  );
}
