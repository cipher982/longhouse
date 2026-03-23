"""Example builtin job for OSS users.

This file demonstrates the job registration pattern. It is NOT imported by default
and won't run in production. To enable it, add an import in register_all_jobs().

For external/private jobs, create a manifest.py in your git repo that imports
JobConfig and job_registry from zerg. See zerg/jobs/__init__.py for details.
"""

from __future__ import annotations

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry


async def run() -> dict[str, str]:
    """Job entry point - must be async and return a dict."""
    return {"status": "ok", "message": "hello from zerg"}


# Self-registration: import this module to register the job
job_registry.register(
    JobConfig(
        id="example-hello-world",
        cron="0 * * * *",  # Every hour
        func=run,
        description="Example builtin job for OSS users",
        tags=["example"],
        enabled=False,  # Disabled by default - enable in your config
    )
)
