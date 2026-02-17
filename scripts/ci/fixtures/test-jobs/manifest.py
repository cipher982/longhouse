"""CI test manifest — registers a simple echo job for E2E testing."""

from zerg.jobs.registry import JobConfig, job_registry

from jobs.echo_job import run as echo_run

job_registry.register(
    JobConfig(
        id="ci-echo-test",
        cron="0 0 1 1 *",  # Once a year (never triggers in CI)
        func=echo_run,
        enabled=True,
        timeout_seconds=30,
        description="CI test job that verifies dep install and job execution",
    )
)
