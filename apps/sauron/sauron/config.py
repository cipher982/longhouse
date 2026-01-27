"""Sauron configuration.

Environment variables for the standalone scheduler service.
Most job infrastructure comes from zerg.jobs - this is just local config.
"""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class SauronSettings:
    """Sauron-specific settings (beyond zerg.jobs defaults)."""

    # API settings
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # Email settings (AWS SES)
    aws_ses_access_key_id: str | None = None
    aws_ses_secret_access_key: str | None = None
    aws_ses_region: str = "us-east-1"
    from_email: str = "Sauron <sauron@drose.io>"
    notify_email: str = "david010@gmail.com"
    alert_email: str = "david010@gmail.com"
    digest_email: str = "david010@gmail.com"

    # Git sync settings (for sauron-jobs repo)
    jobs_git_repo_url: str | None = None
    jobs_git_token: str | None = None
    jobs_git_branch: str = "main"
    jobs_dir: str = "/opt/sauron-jobs"
    jobs_refresh_interval_seconds: int = 300  # 5 minutes

    # Database URL (for job queue and ops.runs)
    database_url: str | None = None

    # Feature flags
    job_queue_enabled: bool = True

    # Log level
    log_level: str = "INFO"


@lru_cache
def get_settings() -> SauronSettings:
    """Load settings from environment variables."""
    return SauronSettings(
        # API
        api_host=os.getenv("API_HOST", "0.0.0.0"),
        api_port=int(os.getenv("API_PORT", "8080")),
        # Email
        aws_ses_access_key_id=os.getenv("AWS_SES_ACCESS_KEY_ID"),
        aws_ses_secret_access_key=os.getenv("AWS_SES_SECRET_ACCESS_KEY"),
        aws_ses_region=os.getenv("AWS_SES_REGION", "us-east-1"),
        from_email=os.getenv("FROM_EMAIL", "Sauron <sauron@drose.io>"),
        notify_email=os.getenv("NOTIFY_EMAIL", "david010@gmail.com"),
        alert_email=os.getenv("ALERT_EMAIL", "david010@gmail.com"),
        digest_email=os.getenv("DIGEST_EMAIL", "david010@gmail.com"),
        # Git sync
        jobs_git_repo_url=os.getenv("JOBS_GIT_REPO_URL"),
        jobs_git_token=os.getenv("JOBS_GIT_TOKEN"),
        jobs_git_branch=os.getenv("JOBS_GIT_BRANCH", "main"),
        jobs_dir=os.getenv("JOBS_DIR", "/opt/sauron-jobs"),
        jobs_refresh_interval_seconds=int(os.getenv("JOBS_REFRESH_INTERVAL_SECONDS", "300")),
        # Database
        database_url=os.getenv("DATABASE_URL"),
        # Feature flags
        job_queue_enabled=os.getenv("JOB_QUEUE_ENABLED", "1").lower() in ("1", "true", "yes"),
        # Logging
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
