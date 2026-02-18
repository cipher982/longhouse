import { useNavigate } from "react-router-dom";
import { toast } from "react-hot-toast";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  PageShell,
  SectionHeader,
  Spinner,
  Table,
} from "../components/ui";
import {
  useDisableJob,
  useEnableJob,
  useJobs,
  useJobsRepoStatus,
  useRecentJobRuns,
} from "../hooks/useJobSecrets";
import type { JobRunHistoryInfo } from "../services/api/jobSecrets";
import { ApiError } from "../services/api/base";
import RepoConnectPanel from "../components/jobs/RepoConnectPanel";

function runStatusVariant(status: string): "success" | "error" | "warning" | "neutral" {
  switch (status) {
    case "success":
      return "success";
    case "failure":
    case "dead":
      return "error";
    case "timeout":
      return "warning";
    default:
      return "neutral";
  }
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatDuration(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rem = seconds % 60;
  return `${minutes}m ${rem}s`;
}

export default function JobsPage() {
  const navigate = useNavigate();
  const { data: jobs, isLoading: jobsLoading, error: jobsError } = useJobs();
  const { data: repo, isLoading: repoLoading, error: repoError } = useJobsRepoStatus();
  const { data: recentRunsData, isLoading: runsLoading } = useRecentJobRuns(50);
  const enableMutation = useEnableJob();
  const disableMutation = useDisableJob();

  const allJobs = jobs ?? [];
  const allRuns = recentRunsData?.runs ?? [];

  // Build a map of job_id -> most recent run for the "Last Run" column
  const lastRunByJob = new Map<string, JobRunHistoryInfo>();
  for (const run of allRuns) {
    if (!lastRunByJob.has(run.job_id)) {
      lastRunByJob.set(run.job_id, run);
    }
  }
  const toggling = enableMutation.isPending || disableMutation.isPending;

  const handleToggle = (jobId: string, enabled: boolean) => {
    if (enabled) {
      disableMutation.mutate(jobId);
      return;
    }
    enableMutation.mutate(
      { jobId },
      {
        onError: (error) => {
          if (error instanceof ApiError && error.status === 409) {
            const body = error.body as Record<string, unknown> | null;
            const detail = body?.detail as Record<string, unknown> | null;
            const missing = (detail?.missing as string[]) ?? [];
            const missingText = missing.length ? ` Missing: ${missing.join(", ")}` : "";
            toast.error(`Missing required secrets.${missingText}`);
            return;
          }
          toast.error(`Failed to enable job: ${error.message}`);
        },
      },
    );
  };

  return (
    <PageShell size="wide">
      <SectionHeader
        title="Jobs"
        description="Monitor scheduled jobs, repo status, and runtime configuration."
        actions={
          <Button variant="secondary" size="sm" onClick={() => navigate("/settings/secrets")}>
            Manage Secrets
          </Button>
        }
      />

      <div className="settings-stack settings-stack--lg">
        <div className="repo-connect-panel">
          <RepoConnectPanel />
        </div>

        <Card>
          <Card.Header>
            <h3 className="settings-section-title">Jobs Repo</h3>
          </Card.Header>
          <Card.Body>
            {repoLoading ? (
              <div className="settings-stack settings-stack--md">
                <Spinner size="sm" />
                <span className="text-muted">Loading repo status...</span>
              </div>
            ) : repoError ? (
              <EmptyState
                title="Failed to load jobs repo"
                description={String(repoError)}
                variant="error"
              />
            ) : repo ? (
              <div className="settings-stack settings-stack--md">
                <div>
                  <strong>Initialized</strong>{" "}
                  <Badge variant={repo.initialized ? "success" : "warning"}>
                    {repo.initialized ? "ready" : "missing"}
                  </Badge>
                </div>
                <div>
                  <strong>Jobs Dir</strong> <span className="text-muted">{repo.jobs_dir}</span>
                </div>
                <div>
                  <strong>Remote</strong> <span className="text-muted">{repo.remote_url ?? "local only"}</span>
                </div>
                <div>
                  <strong>Last Commit</strong>{" "}
                  <span className="text-muted">{repo.last_commit_message ?? "—"}</span>
                </div>
              </div>
            ) : (
              <EmptyState title="No repo status" description="Jobs repo status not available." />
            )}
          </Card.Body>
        </Card>

        <Card>
          <Card.Header>
            <h3 className="settings-section-title">Scheduled Jobs</h3>
          </Card.Header>
          <Card.Body>
            {jobsLoading ? (
              <div className="settings-stack settings-stack--md">
                <Spinner size="sm" />
                <span className="text-muted">Loading jobs...</span>
              </div>
            ) : jobsError ? (
              <EmptyState
                title="Failed to load jobs"
                description={String(jobsError)}
                variant="error"
              />
            ) : !allJobs.length ? (
              <div className="settings-stack settings-stack--lg">
                <div>
                  <h3 className="settings-section-title">Set up your first scheduled job</h3>
                  <p className="text-muted" style={{ marginTop: "0.25rem" }}>
                    Run Python scripts on a schedule — process data, send reports, sync services.
                    Jobs run in your private instance with access to secrets and email notifications.
                  </p>
                </div>
                <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
                  <Card style={{ flex: "1 1 0", minWidth: "240px" }}>
                    <Card.Body>
                      <div className="settings-stack settings-stack--md">
                        <div>
                          <Badge variant="success">Recommended</Badge>
                        </div>
                        <strong>Start from template</strong>
                        <p className="text-muted">
                          Fork our starter repo with a working example job and manifest.
                        </p>
                        <Button
                          variant="primary"
                          size="sm"
                          onClick={() =>
                            window.open(
                              "https://github.com/cipher982/longhouse-jobs-template",
                              "_blank",
                              "noopener",
                            )
                          }
                        >
                          View Template
                        </Button>
                      </div>
                    </Card.Body>
                  </Card>
                  <Card style={{ flex: "1 1 0", minWidth: "240px" }}>
                    <Card.Body>
                      <div className="settings-stack settings-stack--md">
                        <strong>Connect your repo</strong>
                        <p className="text-muted">
                          Already have a jobs repo? Connect it above to sync your jobs.
                        </p>
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() =>
                            document
                              .querySelector(".repo-connect-panel")
                              ?.scrollIntoView({ behavior: "smooth" })
                          }
                        >
                          Go to Connect
                        </Button>
                      </div>
                    </Card.Body>
                  </Card>
                </div>
              </div>
            ) : (
              <Table>
                <Table.Header>
                  <Table.Cell isHeader>Job</Table.Cell>
                  <Table.Cell isHeader>Cron</Table.Cell>
                  <Table.Cell isHeader>Status</Table.Cell>
                  <Table.Cell isHeader>Last Run</Table.Cell>
                  <Table.Cell isHeader>Secrets</Table.Cell>
                  <Table.Cell isHeader>Actions</Table.Cell>
                </Table.Header>
                <Table.Body>
                  {allJobs.map((job) => (
                    <Table.Row key={job.id}>
                      <Table.Cell>
                        <div><strong>{job.id}</strong></div>
                        <div className="text-muted">{job.description}</div>
                      </Table.Cell>
                      <Table.Cell>{job.cron}</Table.Cell>
                      <Table.Cell>
                        <Badge variant={job.enabled ? "success" : "neutral"}>
                          {job.enabled ? "enabled" : "disabled"}
                        </Badge>
                      </Table.Cell>
                      <Table.Cell>
                        {(() => {
                          const lastRun = lastRunByJob.get(job.id);
                          if (!lastRun) return "—";
                          return (
                            <span>
                              <Badge variant={runStatusVariant(lastRun.status)}>
                                {lastRun.status}
                              </Badge>{" "}
                              <span className="text-muted">{relativeTime(lastRun.started_at)}</span>
                            </span>
                          );
                        })()}
                      </Table.Cell>
                      <Table.Cell>
                        {job.secrets.length > 0 ? `${job.secrets.length} declared` : "—"}
                      </Table.Cell>
                      <Table.Cell>
                        <Button
                          variant={job.enabled ? "ghost" : "primary"}
                          size="sm"
                          onClick={() => handleToggle(job.id, job.enabled)}
                          disabled={toggling}
                        >
                          {toggling ? "..." : job.enabled ? "Disable" : "Enable"}
                        </Button>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            )}
          </Card.Body>
        </Card>
        <Card>
          <Card.Header>
            <h3 className="settings-section-title">Recent Runs</h3>
          </Card.Header>
          <Card.Body>
            {runsLoading ? (
              <div className="settings-stack settings-stack--md">
                <Spinner size="sm" />
                <span className="text-muted">Loading recent runs...</span>
              </div>
            ) : !allRuns.length ? (
              <EmptyState
                title="No runs yet"
                description="Job runs will appear here once jobs start executing."
              />
            ) : (
              <Table>
                <Table.Header>
                  <Table.Cell isHeader>Job</Table.Cell>
                  <Table.Cell isHeader>Status</Table.Cell>
                  <Table.Cell isHeader>Started</Table.Cell>
                  <Table.Cell isHeader>Duration</Table.Cell>
                  <Table.Cell isHeader>Error</Table.Cell>
                </Table.Header>
                <Table.Body>
                  {allRuns.slice(0, 10).map((run) => (
                    <Table.Row key={run.id}>
                      <Table.Cell>{run.job_id}</Table.Cell>
                      <Table.Cell>
                        <Badge variant={runStatusVariant(run.status)}>{run.status}</Badge>
                      </Table.Cell>
                      <Table.Cell>
                        <span className="text-muted">{relativeTime(run.started_at)}</span>
                      </Table.Cell>
                      <Table.Cell>{formatDuration(run.duration_ms)}</Table.Cell>
                      <Table.Cell>
                        {run.error_message ? (
                          <span className="text-muted" title={run.error_message}>
                            {run.error_message.length > 60
                              ? `${run.error_message.slice(0, 60)}...`
                              : run.error_message}
                          </span>
                        ) : (
                          "—"
                        )}
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            )}
          </Card.Body>
        </Card>
      </div>
    </PageShell>
  );
}
