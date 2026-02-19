import { useEffect, useState } from "react";
import { PageShell, SectionHeader, EmptyState, Button, Badge, Spinner } from "../components/ui";
import { useProposals, useApproveProposal, useDeclineProposal } from "../hooks/useProposals";
import type { ActionProposal } from "../services/api/proposals";
import "./ProposalsPage.css";

const STATUS_TABS = ["pending", "approved", "declined"] as const;

function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const diffMins = Math.floor((now.getTime() - d.getTime()) / 60000);
  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays}d ago`;
}

function severityVariant(severity: string | null): "neutral" | "warning" | "error" {
  if (severity === "critical") return "error";
  if (severity === "warning") return "warning";
  return "neutral";
}

function typeLabel(insightType: string | null): string {
  if (!insightType) return "insight";
  return insightType;
}

function ProposalCard({
  proposal,
  onApprove,
  onDecline,
  isApproving,
  isDeclining,
}: {
  proposal: ActionProposal;
  onApprove: (id: string) => void;
  onDecline: (id: string) => void;
  isApproving: boolean;
  isDeclining: boolean;
}) {
  return (
    <div className="proposal-card" data-status={proposal.status}>
      <div className="proposal-card-header">
        <h3 className="proposal-card-title">{proposal.title}</h3>
        <div className="proposal-card-badges">
          <Badge variant={severityVariant(proposal.severity)}>
            {typeLabel(proposal.insight_type)}
          </Badge>
          {proposal.project && <Badge variant="neutral">{proposal.project}</Badge>}
        </div>
      </div>

      <div className="proposal-card-blurb">{proposal.action_blurb}</div>

      <div className="proposal-card-meta">
        <span>{formatDate(proposal.created_at)}</span>
        {proposal.decided_at && <span>Decided {formatDate(proposal.decided_at)}</span>}
      </div>

      {proposal.status === "pending" && (
        <div className="proposal-card-actions">
          <Button
            variant="success"
            size="sm"
            onClick={() => onApprove(proposal.id)}
            disabled={isApproving || isDeclining}
          >
            {isApproving ? "Approving..." : "Approve"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDecline(proposal.id)}
            disabled={isApproving || isDeclining}
          >
            {isDeclining ? "Declining..." : "Decline"}
          </Button>
        </div>
      )}
    </div>
  );
}

export default function ProposalsPage() {
  const [activeStatus, setActiveStatus] = useState<string>("pending");
  const { data, isLoading, error, refetch } = useProposals({ status: activeStatus });
  const approve = useApproveProposal();
  const decline = useDeclineProposal();

  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute("data-ready", "true");
    }
    return () => document.body.removeAttribute("data-ready");
  }, [isLoading]);

  if (error) {
    return (
      <PageShell size="narrow">
        <EmptyState
          variant="error"
          title="Failed to load proposals"
          description={error.message}
          action={<Button variant="primary" onClick={() => refetch()}>Try Again</Button>}
        />
      </PageShell>
    );
  }

  const proposals = data?.proposals ?? [];

  return (
    <PageShell size="narrow">
      <SectionHeader
        title="Action Proposals"
        description="Review actionable insights from reflection. Approved proposals become tasks for agents."
      />

      <div className="proposals-filters">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab}
            className={`proposals-filter-btn${activeStatus === tab ? " active" : ""}`}
            onClick={() => setActiveStatus(tab)}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
            {activeStatus === tab && data ? (
              <span className="proposals-count">({data.total})</span>
            ) : null}
          </button>
        ))}
      </div>

      {isLoading ? (
        <EmptyState icon={<Spinner size="lg" />} title="Loading proposals..." />
      ) : proposals.length === 0 ? (
        <EmptyState
          title="No proposals"
          description={
            activeStatus === "pending"
              ? "No pending proposals. Reflection runs every 6 hours."
              : `No ${activeStatus} proposals found.`
          }
        />
      ) : (
        <div className="proposals-grid">
          {proposals.map((p) => (
            <ProposalCard
              key={p.id}
              proposal={p}
              onApprove={(id) => approve.mutate(id)}
              onDecline={(id) => decline.mutate(id)}
              isApproving={approve.isPending && approve.variables === p.id}
              isDeclining={decline.isPending && decline.variables === p.id}
            />
          ))}
        </div>
      )}
    </PageShell>
  );
}
