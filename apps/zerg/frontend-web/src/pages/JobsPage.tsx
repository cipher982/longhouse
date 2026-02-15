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
import { useDisableJob, useEnableJob, useJobs, useJobsRepoStatus } from "../hooks/useJobSecrets";
import { ApiError } from "../services/api/base";

export default function JobsPage() {
  const navigate = useNavigate();
  const { data: jobs, isLoading: jobsLoading, error: jobsError } = useJobs();
  const { data: repo, isLoading: repoLoading, error: repoError } = useJobsRepoStatus();
  const enableMutation = useEnableJob();
  const disableMutation = useDisableJob();

  const allJobs = jobs ?? [];
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
              <EmptyState
                title="No jobs registered"
                description="Connect a jobs repo or add jobs to /data/jobs to get started."
              />
            ) : (
              <Table>
                <Table.Header>
                  <Table.Cell isHeader>Job</Table.Cell>
                  <Table.Cell isHeader>Cron</Table.Cell>
                  <Table.Cell isHeader>Status</Table.Cell>
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
      </div>
    </PageShell>
  );
}
