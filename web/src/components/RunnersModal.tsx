import { useState } from "react";
import { useNavigate } from "react-router-dom";
import "../styles/runners-modal.css";
import { useRunners } from "../hooks/useRunners";
import type { Runner } from "../services/api";
import AddRunnerModal from "./AddRunnerModal";
import { useReadinessFlag } from "../lib/readiness-contract";
import { Button, Badge, EmptyState, Spinner } from "./ui";
import { PlusIcon, XIcon } from "./icons";
import { parseUTC } from "../lib/dateUtils";
import {
  formatCompactDuration,
  normalizeRunnerMetadata,
  runnerStatusVariant,
  versionStatusLabel,
} from "../lib/runnerPresentation";

function formatHeartbeatAge(runner: Runner): string {
  if (typeof runner.last_seen_age_seconds === "number") {
    return `${formatCompactDuration(runner.last_seen_age_seconds)} ago`;
  }
  if (!runner.last_seen_at) return "Never";
  const date = parseUTC(runner.last_seen_at);
  const diffMins = Math.floor((Date.now() - date.getTime()) / 60000);
  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  return diffHours < 24 ? `${diffHours}h ago` : `${Math.floor(diffHours / 24)}d ago`;
}

function platformLabel(meta: Runner["runner_metadata"]): string {
  const m = normalizeRunnerMetadata(meta);
  if (!m) return "Unknown";
  const p = m.platform ?? "";
  const a = m.arch ?? "";
  const name = p === "darwin" ? "macOS" : p === "linux" ? "Linux" : p || "Unknown";
  return a ? `${name} · ${a}` : name;
}

function hostname(meta: Runner["runner_metadata"]): string | null {
  return normalizeRunnerMetadata(meta)?.hostname ?? null;
}

function versionBadgeVariant(s: string | null | undefined): "success" | "warning" | "neutral" {
  if (s === "current") return "success";
  if (s === "outdated") return "warning";
  return "neutral";
}

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

function DrawerContent({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners({ refetchInterval: 10_000 });
  const [showAddModal, setShowAddModal] = useState(false);

  useReadinessFlag({ ready: !isLoading });

  const handleRunnerClick = (runner: Runner) => {
    onClose();
    navigate(`/runners/${runner.id}`);
  };

  return (
    <>
      <div className="rmDrawer-header">
        <span className="rmDrawer-title">Machines</span>
        <div className="rmDrawer-header-actions">
          {!isLoading && !error && (
            <Button
              variant="primary"
              size="sm"
              data-testid="runners-add-button"
              onClick={() => setShowAddModal(true)}
            >
              <PlusIcon />
              Connect
            </Button>
          )}
          <button
            type="button"
            className="rmDrawer-close"
            onClick={onClose}
            aria-label="Close"
          >
            <XIcon width={18} height={18} />
          </button>
        </div>
      </div>

      <div className="rmDrawer-body">
        {isLoading && (
          <div className="rmDrawer-centered">
            <Spinner size="lg" />
            <p className="rmDrawer-hint">Loading machines…</p>
          </div>
        )}

        {error && !isLoading && (
          <EmptyState
            variant="error"
            title="Error loading machines"
            description={error instanceof Error ? error.message : "Unknown error"}
          />
        )}

        {!isLoading && !error && runners && runners.length === 0 && (
          <div className="rmDrawer-centered">
            <EmptyState
              title="No machines connected yet"
              description="Connect a laptop, homelab box, Mac mini, or VPS so Longhouse can start sessions and run commands where your work lives."
              action={
                <Button
                  variant="primary"
                  size="lg"
                  data-testid="runners-add-first-button"
                  onClick={() => setShowAddModal(true)}
                >
                  Connect your first machine
                </Button>
              }
            />
          </div>
        )}

        {!isLoading && !error && runners && runners.length > 0 && (
          <ul className="rmDrawer-list">
            {runners.map((runner) => (
              <li
                key={runner.id}
                className={`rmDrawer-row rmDrawer-row--${runner.status}`}
                role="button"
                tabIndex={0}
                onClick={() => handleRunnerClick(runner)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") handleRunnerClick(runner);
                }}
                data-testid={`runner-card-${runner.id}`}
              >
                <div className="rmDrawer-row-left">
                  <span className={`rmDrawer-dot rmDrawer-dot--${runner.status}`} />
                  <div className="rmDrawer-row-info">
                    <span className="rmDrawer-row-name">{runner.name}</span>
                    {hostname(runner.runner_metadata) && (
                      <span className="rmDrawer-row-host">{hostname(runner.runner_metadata)}</span>
                    )}
                    <span className="rmDrawer-row-meta">
                      {platformLabel(runner.runner_metadata)}
                      {" · "}
                      {formatHeartbeatAge(runner)}
                    </span>
                  </div>
                </div>
                <div className="rmDrawer-row-right">
                  <Badge variant={runnerStatusVariant(runner.status)}>{runner.status}</Badge>
                  {versionStatusLabel(runner.version_status) && (
                    <Badge variant={versionBadgeVariant(runner.version_status)}>
                      {versionStatusLabel(runner.version_status)}
                    </Badge>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {showAddModal && (
        <AddRunnerModal isOpen onClose={() => setShowAddModal(false)} />
      )}
    </>
  );
}

export default function RunnersModal({ isOpen, onClose }: Props) {
  if (!isOpen) return null;

  return (
    <div
      className="rmDrawer-overlay"
      onClick={onClose}
      aria-modal="true"
      role="dialog"
      aria-label="Machines"
      data-testid="runners-modal"
    >
      <div
        className="rmDrawer-panel"
        onClick={(e) => e.stopPropagation()}
      >
        <DrawerContent onClose={onClose} />
      </div>
    </div>
  );
}
