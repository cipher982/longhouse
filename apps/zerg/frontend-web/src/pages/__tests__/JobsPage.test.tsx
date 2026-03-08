import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TestRouter } from "../../test/test-utils";

import JobsPage from "../JobsPage";

const hooks = vi.hoisted(() => ({
  useJobsWithMeta: vi.fn(),
  useJobsRepoStatus: vi.fn(),
  useRecentJobRuns: vi.fn(),
  useLastJobRuns: vi.fn(),
  useEnableJob: vi.fn(),
  useDisableJob: vi.fn(),
}));

vi.mock("../../hooks/useJobSecrets", () => hooks);
vi.mock("../../components/jobs/RepoConnectPanel", () => ({
  default: () => <div data-testid="repo-connect-panel">Repo Connect Panel</div>,
}));

function renderJobsPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter>
        <JobsPage />
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("JobsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    hooks.useJobsWithMeta.mockReturnValue({
      data: {
        jobs: [
          {
            id: "daily-digest",
            cron: "0 8 * * *",
            enabled: true,
            timeout_seconds: 300,
            max_attempts: 3,
            tags: [],
            project: null,
            description: "Send daily digest email",
            secrets: [],
          },
        ],
        total: 1,
        registration_warnings: ["Failed to import zerg.jobs.daily_digest"],
      },
      isLoading: false,
      error: null,
    });
    hooks.useJobsRepoStatus.mockReturnValue({
      data: {
        initialized: true,
        has_remote: false,
        remote_url: null,
        last_commit_time: null,
        last_commit_message: null,
        jobs_dir: "/tmp/jobs",
        job_count: 1,
      },
      isLoading: false,
      error: null,
    });
    hooks.useRecentJobRuns.mockReturnValue({
      data: { runs: [], total: 0 },
      isLoading: false,
      error: null,
    });
    hooks.useLastJobRuns.mockReturnValue({
      data: { last_runs: {} },
      isLoading: false,
      error: null,
    });
    hooks.useEnableJob.mockReturnValue({
      isPending: false,
      mutate: vi.fn(),
    });
    hooks.useDisableJob.mockReturnValue({
      isPending: false,
      mutate: vi.fn(),
    });
  });

  it("shows backend registration warnings above the jobs table", async () => {
    renderJobsPage();

    expect(await screen.findByText("Registration Warnings")).toBeInTheDocument();
    expect(screen.getByText("Failed to import zerg.jobs.daily_digest")).toBeInTheDocument();
    expect(screen.getByText("daily-digest")).toBeInTheDocument();
  });


  it("renders degraded last-run badges as warning state", async () => {
    hooks.useLastJobRuns.mockReturnValue({
      data: {
        last_runs: {
          "daily-digest": {
            id: "run-1",
            job_id: "daily-digest",
            status: "degraded",
            started_at: "2026-03-08T02:00:00Z",
            finished_at: "2026-03-08T02:01:00Z",
            duration_ms: 60000,
            error_message: "1 pipeline step degraded",
            error_type: "PartialFailure",
            created_at: "2026-03-08T02:01:00Z",
          },
        },
      },
      isLoading: false,
      error: null,
    });

    renderJobsPage();

    const badge = await screen.findByText("degraded");
    expect(badge).toHaveClass("ui-badge--warning");
    expect(screen.getByText("PartialFailure")).toBeInTheDocument();
  });
});
