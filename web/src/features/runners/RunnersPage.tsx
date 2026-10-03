import { useState } from "react";
import { useNavigate } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { useRunners } from "./useRunners";
import { listMachines, type Runner } from "@/shared/api/index";
import MachineAgents from "@/features/machines/MachineAgents";
import AddRunnerModal from "./AddRunnerModal";
import { useReadinessFlag } from "@/shared/lib/readiness-contract";
import {
  Button,
  Badge,
  Card,
  SectionHeader,
  EmptyState,
  PageShell,
  Spinner
} from "@/shared/ui";
import { PlusIcon } from "@/shared/ui/icons";
import {
  formatRunnerVersionValue,
  normalizeRunnerMetadata,
  runnerStatusVariant,
  updatePolicyLabel,
  versionStatusLabel,
} from "./runnerPresentation";
import {
  formatHeartbeatAge,
  formatHeartbeatThreshold,
  formatVersionHint,
  getVersionVariant,
  installLayoutHint,
  installLayoutLabel,
  updatePolicyHint,
} from "./runnerUtils";
import "./runners.css";

function platformLabel(meta: Runner["runner_metadata"]): string {
  const metadata = normalizeRunnerMetadata(meta);
  if (!metadata) return "Unknown";

  const p = metadata.platform ?? "";
  const a = metadata.arch ?? "";
  const platName = p === "darwin" ? "macOS" : p === "linux" ? "Linux" : p || "Unknown";
  return a ? `${platName} · ${a}` : platName;
}

function hostname(meta: Runner["runner_metadata"]): string | null {
  return normalizeRunnerMetadata(meta)?.hostname ?? null;
}

function fallbackStatusSummary(status: string): string {
  switch (status) {
    case "online":
      return "Online. Live runner connection is active.";
    case "revoked":
      return "Revoked. This runner cannot reconnect.";
    default:
      return "Offline. No live runner connection is active.";
  }
}

export default function RunnersPage() {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners({ refetchInterval: 10_000 });
  // The Machine Agents connected to this Runtime Host: what "my machine" means
  // to a newcomer. A failed lookup shows no list rather than an error page.
  const {
    data: machineDirectory,
    isLoading: machinesLoading,
    isError: machinesError,
  } = useQuery({
    queryKey: ["machine-directory"],
    queryFn: listMachines,
    refetchInterval: 10_000,
  });
  const machines = machineDirectory?.machines ?? [];
  const [showAddModal, setShowAddModal] = useState(false);

  // Ready signal - indicates page is interactive (even if empty)
  useReadinessFlag({ ready: !isLoading && !machinesLoading });

  if (isLoading) {
    return (
      <div className="runners-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading machines..."
          description="Fetching your connected machines."
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="runners-page-container">
        <EmptyState
          variant="error"
          title="Error loading machines"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      </div>
    );
  }

  return (
    <PageShell size="wide" className="runners-page-container">
      <div className="runners-page">
        <SectionHeader
          title="Machines"
          description="The machines Longhouse imports sessions from and starts sessions on."
          actions={
            <Button variant="primary" data-testid="runners-add-button" onClick={() => setShowAddModal(true)}>
              <PlusIcon />
              Connect a machine
            </Button>
          }
        />

        {machines.length > 0 && <MachineAgents machines={machines} />}
        {machines.length > 0 && <h3 className="runners-subheading">Runners</h3>}

        {machines.length === 0 && runners && runners.length === 0 ? (
          // Held until the machine lookup settles, so a connected machine never
          // flashes "No machines connected yet" on its way in; a failed lookup
          // is an outage, not an empty host.
          machinesLoading ? null : machinesError ? (
            <EmptyState
              variant="error"
              title="Could not load connected machines"
              description="Longhouse could not read this host's machine list. Reload to try again."
            />
          ) : <EmptyState
            title="No machines connected yet"
            description="Connect a machine. New sessions you start from now on appear here by default; choose older project history later with longhouse machine scope. A Runner is an optional extra for running shell commands on one from the browser."
            action={
              <Button variant="primary" size="lg" data-testid="runners-add-first-button" onClick={() => setShowAddModal(true)}>
                Connect a machine
              </Button>
            }
          />
        ) : runners && runners.length === 0 ? (
          <p className="runners-none" data-testid="runners-none">
            No Runners. A Runner is an optional extra for running shell commands on a machine from the browser.
          </p>
        ) : (
          <div className="runners-grid">
            {runners?.map((runner) => (
              <Card
                key={runner.id}
                className={`runner-card runner-card--${runner.status}`}
                onClick={() => navigate(`/runners/${runner.id}`)}
                data-testid={`runner-card-${runner.id}`}
              >
                <Card.Header className="runner-card-header">
                  <div className="runner-card-title-group">
                    <div className="runner-card-name-row">
                      <span className={`runner-status-dot runner-status-dot--${runner.status}`} />
                      <h3 className="runner-card-title">{runner.name}</h3>
                    </div>
                    {hostname(runner.runner_metadata) && (
                      <span className="runner-card-hostname">
                        {hostname(runner.runner_metadata)}
                      </span>
                    )}
                  </div>
                  <Badge variant={runnerStatusVariant(runner.status)}>
                    {runner.status}
                  </Badge>
                </Card.Header>

                <Card.Body>
                  <div className="runner-card-health">
                    <p className="runner-card-summary">
                      {runner.status_summary ?? fallbackStatusSummary(runner.status)}
                    </p>
                    <div className="runner-card-flags">
                      {runner.status_reason && (
                        <span className="runner-inline-pill runner-inline-pill--code">
                          {runner.status_reason}
                        </span>
                      )}
                      {versionStatusLabel(runner.version_status) && (
                        <span className={`runner-inline-pill runner-inline-pill--${getVersionVariant(runner.version_status)}`}>
                          {versionStatusLabel(runner.version_status)}
                        </span>
                      )}
                      {runner.capabilities_match === false && (
                        <span className="runner-inline-pill runner-inline-pill--warning">
                          capability mismatch
                        </span>
                      )}
                      {!runner.managed_install_ready && (
                        <span className="runner-inline-pill runner-inline-pill--warning">
                          legacy layout
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="runner-card-details">
                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Platform</span>
                      <span className="runner-detail-value">
                        {platformLabel(runner.runner_metadata)}
                      </span>
                    </div>

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Heartbeat</span>
                      <div className="runner-detail-stack">
                        <span className="runner-detail-value">
                          {formatHeartbeatAge(runner)}
                        </span>
                        {typeof runner.stale_after_seconds === "number" && (
                          <span className="runner-detail-subvalue">
                            {formatHeartbeatThreshold(runner.stale_after_seconds)}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Version</span>
                      <div className="runner-detail-stack">
                        <span className="runner-detail-value">{formatRunnerVersionValue(runner)}</span>
                        {formatVersionHint(runner) && (
                          <span className="runner-detail-subvalue">{formatVersionHint(runner)}</span>
                        )}
                      </div>
                    </div>

                    {runner.install_mode && (
                      <div className="runner-detail-row">
                        <span className="runner-detail-label">Install</span>
                        <span className="runner-detail-value">{runner.install_mode}</span>
                      </div>
                    )}

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Updates</span>
                      <div className="runner-detail-stack">
                        <span className="runner-detail-value">{updatePolicyLabel(runner.auto_update_policy)}</span>
                        <span className="runner-detail-subvalue">
                          {installLayoutLabel(runner)}. {runner.managed_install_ready ? updatePolicyHint(runner.auto_update_policy) : installLayoutHint(runner)}
                        </span>
                      </div>
                    </div>

                    {runner.reported_capabilities && runner.capabilities_match === false && (
                      <div className="runner-detail-row">
                        <span className="runner-detail-label">Runner reported</span>
                        <span className="runner-detail-value runner-detail-value--inline-list">
                          {runner.reported_capabilities.join(", ")}
                        </span>
                      </div>
                    )}

                    {runner.capabilities && runner.capabilities.length > 0 && (
                      <div className="runner-detail-row">
                        <span className="runner-detail-label">Capabilities</span>
                        <div className="capabilities-list">
                          {runner.capabilities.map((cap) => (
                            <span key={cap} className="capability-chip">
                              {cap}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}

                  </div>
                </Card.Body>
              </Card>
            ))}
          </div>
        )}
      </div>

      {showAddModal && (
        <AddRunnerModal
          isOpen={showAddModal}
          onClose={() => setShowAddModal(false)}
        />
      )}
    </PageShell>
  );
}
