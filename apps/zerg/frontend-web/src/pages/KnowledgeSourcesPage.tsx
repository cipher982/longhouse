import { useState, useEffect } from "react";
import {
  useKnowledgeSources,
  useDeleteKnowledgeSource,
  useSyncKnowledgeSource,
  useCreateKnowledgeSource,
} from "../hooks/useKnowledgeSources";
import { KnowledgeSourceCard } from "../components/KnowledgeSourceCard";
import { AddKnowledgeSourceModal } from "../components/AddKnowledgeSourceModal";
import { AddContextModal } from "../components/AddContextModal";
import { KnowledgeSearchPanel } from "../components/KnowledgeSearchPanel";
import {
  Button,
  SectionHeader,
  EmptyState,
  Spinner,
  PageShell
} from "../components/ui";
import { PlusIcon } from "../components/icons";
import { useConfirm } from "../components/confirm";
import "../styles/knowledge-sources.css";

export default function KnowledgeSourcesPage() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isContextModalOpen, setIsContextModalOpen] = useState(false);
  const [syncingIds, setSyncingIds] = useState<Set<number>>(new Set());
  const confirm = useConfirm();

  const { data: sources, isLoading, error } = useKnowledgeSources();
  const deleteMutation = useDeleteKnowledgeSource();
  const syncMutation = useSyncKnowledgeSource();
  const createMutation = useCreateKnowledgeSource();

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

  const handleDelete = async (id: number) => {
    const confirmed = await confirm({
      title: 'Delete knowledge source?',
      message: 'This will permanently remove this knowledge source and all indexed content.',
      confirmLabel: 'Delete',
      cancelLabel: 'Keep',
      variant: 'danger',
    });
    if (confirmed) {
      deleteMutation.mutate(id);
    }
  };

  const handleContextSubmit = async (title: string, content: string) => {
    await createMutation.mutateAsync({
      name: title,
      source_type: "user_text",
      config: { content } as unknown as Record<string, never>,
    });
  };

  // Ready signal - indicates page is interactive (even if empty)
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute('data-ready', 'true');
    }
    return () => document.body.removeAttribute('data-ready');
  }, [isLoading]);

  if (isLoading) {
    return (
      <PageShell size="normal" className="knowledge-sources-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading knowledge sources..."
          description="Fetching your connected documentation and codebases."
        />
      </PageShell>
    );
  }

  if (error) {
    return (
      <PageShell size="normal" className="knowledge-sources-page-container">
        <EmptyState
          variant="error"
          title="Error loading knowledge sources"
          description={String(error)}
        />
      </PageShell>
    );
  }

  return (
    <PageShell size="normal" className="knowledge-sources-page-container">
      <SectionHeader
        title="Knowledge Sources"
        description="Connect knowledge sources to give your fiches context about your codebase, documentation, and more."
        actions={
          <div className="knowledge-sources-actions">
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
    </PageShell>
  );
}
