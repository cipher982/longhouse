"""QA Fiche job - AI-powered monitoring for Swarmlet health.

Runs every 15 minutes to:
- Collect system health metrics
- Detect anomalies via AI analysis
- Alert on chronic issues via Discord
"""

from zerg.jobs.qa.qa_fiche import run
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry

job_registry.register(
    JobConfig(
        id="zerg-qa",
        cron="*/15 * * * *",  # Every 15 minutes
        func=run,
        timeout_seconds=600,  # 10 minutes max
        max_attempts=1,  # Don't retry - AI analysis is expensive
        tags=["zerg", "qa", "agentic", "monitoring"],
        project="zerg",
        description="AI QA fiche - monitors Zerg health and detects anomalies",
    )
)

__all__ = ["run"]
