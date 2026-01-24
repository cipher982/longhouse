"""Backup Sentinel - Proactive Kopia backup monitoring.

Monitors backup freshness and AI validation results across all configured hosts.
Migrated from Sauron.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel
from pydantic import Field

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.shared.email import send_alert_email
from zerg.shared.ssh import run_ssh_command

logger = logging.getLogger(__name__)

# --- OpenAI Configuration ---

OPENAI_MODEL = "gpt-5-mini"


class BackupAssessmentResponse(BaseModel):
    """Structured response from AI backup log analysis."""

    status: Literal["success", "warning", "critical_failure"] = Field(
        description="Overall backup health: success (all good), warning (minor issues), critical_failure (needs attention)"
    )
    severity: int = Field(ge=0, le=10, description="Severity score 0-10 (0=perfect, 10=complete failure)")
    alert: Literal["yes", "no"] = Field(description="Whether this warrants an alert email")
    summary: str = Field(description="One sentence summary of backup health for the email report")


# --- Configuration defaults (overridable via BACKUP_CONFIG_PATH) ---

# Docker gateway IP to reach host SSH from inside container
DOCKER_HOST_SSH = "172.29.0.1"

# Tailscale IPs (SSH config on hosts overrides hostnames to public IPs)
CUBE_TAILSCALE_IP = "100.104.187.47"

# Tailscale hosts that need warm-up before SSH (path discovery can take >10s after idle)
TAILSCALE_WARMUP_HOSTS = ["cube", "cinder"]

DEFAULT_BACKUP_CONFIG = {
    "hosts": [
        {
            "id": "clifford",
            "hostname": DOCKER_HOST_SSH,  # Use gateway IP, not Tailscale hostname
            "user": "root",
            "path": "/",
            "threshold_hours": 26,
        },
        {
            "id": "cube",
            "hostname": CUBE_TAILSCALE_IP,
            "user": "root",
            "port": 2222,
            "path": "/",
            "threshold_hours": 26,
            "bastion_host": f"root@{DOCKER_HOST_SSH}",
        },
        {
            "id": "cinder",
            "hostname": "cinder",  # Tailscale MagicDNS works for this one
            "user": "davidrose",
            "path": "*",  # Multiple paths backed up, check all
            "threshold_hours": 30,
            "bastion_host": f"root@{DOCKER_HOST_SSH}",
            "kopia_path": "/opt/homebrew/bin/kopia",
            "kopia_password_file": "~/.config/kopia/.password",
            "skip_ai_assessment": True,  # macOS has no journalctl
        },
    ]
}


def _load_config_from_path(path: Path) -> dict[str, Any]:
    """Load backup config JSON from disk."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Backup config must be a JSON object with 'hosts'")
    if "hosts" not in data or not isinstance(data["hosts"], list):
        raise ValueError("Backup config missing 'hosts' list")
    return data


def load_backup_config() -> dict[str, Any]:
    """
    Return backup configuration.

    If BACKUP_CONFIG_PATH is set, load JSON from that path.
    Otherwise, return DEFAULT_BACKUP_CONFIG.
    """
    path_str = os.getenv("BACKUP_CONFIG_PATH")
    if not path_str:
        return DEFAULT_BACKUP_CONFIG

    path = Path(path_str)
    try:
        config = _load_config_from_path(path)
        logger.info("Loaded backup config from %s", path)
        return config
    except Exception as exc:
        logger.error("Failed to load BACKUP_CONFIG_PATH=%s: %s. Falling back to defaults.", path, exc)
        return DEFAULT_BACKUP_CONFIG


# --- Tailscale Warm-up ---


async def warm_up_tailscale_paths() -> None:
    """
    Warm up Tailscale paths to remote hosts before SSH checks.

    After hours of inactivity (like at 4 AM), Tailscale connections are "cold" -
    the first connection triggers path discovery (DERP relay negotiation, NAT traversal)
    which can take >10 seconds. This causes SSH connectivity tests to timeout.

    By running `tailscale ping` from the bastion host (clifford) to each Tailscale
    host first, we force path establishment before the SSH timeout pressure kicks in.
    """
    if not TAILSCALE_WARMUP_HOSTS:
        return

    logger.info("Warming up Tailscale paths to: %s", TAILSCALE_WARMUP_HOSTS)

    # Build command to ping all hosts in parallel from clifford
    ping_commands = [f"tailscale ping --c 1 {host}" for host in TAILSCALE_WARMUP_HOSTS]
    combined_command = " & ".join(ping_commands) + " & wait"

    result = await asyncio.to_thread(
        run_ssh_command,
        DOCKER_HOST_SSH,
        combined_command,
        user="root",
        timeout=30,  # Give Tailscale time to establish paths
    )

    if result.success:
        logger.info("Tailscale warm-up complete: %s", result.stdout.strip()[:200])
    else:
        # Warm-up failure is not fatal - SSH might still work if paths are already warm
        logger.warning("Tailscale warm-up had issues (non-fatal): %s", result.stderr[:200])


# --- Data Models ---


@dataclass
class AIAssessment:
    """AI assessment from backup validation."""

    status: str  # success, warning, critical_failure
    severity: int  # 0-10
    alert: str  # yes/no
    summary: str | None = None


@dataclass
class HostBackupStatus:
    """Backup status for a single host."""

    host_id: str
    hostname: str
    reachable: bool
    last_snapshot: datetime | None = None
    age_hours: float | None = None
    threshold_hours: int = 24
    is_stale: bool = False
    ai_assessment: AIAssessment | None = None
    error_message: str | None = None

    @property
    def overall_status(self) -> str:
        """Determine overall status: success, warning, critical."""
        if not self.reachable:
            return "critical"
        if self.is_stale:
            return "critical"
        if self.ai_assessment:
            if self.ai_assessment.status == "critical_failure":
                return "critical"
            if self.ai_assessment.status == "warning":
                return "warning"
        if self.last_snapshot is None:
            return "critical"
        return "success"


# --- Remote Data Extraction ---


async def fetch_last_snapshot_time(
    hostname: str,
    user: str,
    path: str,
    timeout: int = 60,
    port: int = 22,
    bastion_host: str | None = None,
    kopia_path: str = "kopia",
    kopia_password_file: str | None = None,
) -> datetime | None:
    """
    Fetch the most recent snapshot timestamp from a remote host.

    Executes: kopia snapshot list {path} --json
    Parses: [.[] | select(.incomplete == null) | .startTime] | max
    """
    # Build kopia command, optionally with password from file
    # Use shlex.quote to prevent shell injection from user-controlled paths
    list_all = path == "*"
    path_arg = "--all" if list_all else shlex.quote(path)
    safe_kopia_path = shlex.quote(kopia_path)
    if kopia_password_file:
        safe_password_file = shlex.quote(kopia_password_file)
        command = f"KOPIA_PASSWORD=$(cat {safe_password_file}) {safe_kopia_path} snapshot list {path_arg} --json"
    else:
        command = f"{safe_kopia_path} snapshot list {path_arg} --json"

    logger.debug("Fetching snapshot list from %s:%s", hostname, path)

    result = await asyncio.to_thread(
        run_ssh_command,
        hostname,
        command,
        user=user,
        port=port,
        timeout=timeout,
        bastion_host=bastion_host,
    )

    if not result.success:
        logger.warning("Failed to fetch snapshots from %s: %s", hostname, result.stderr[:200])
        return None

    try:
        snapshots = json.loads(result.stdout)
        if not isinstance(snapshots, list):
            logger.error("Unexpected JSON format from %s: not a list", hostname)
            return None

        # Filter out incomplete snapshots and extract startTime
        complete_snapshots = [s for s in snapshots if isinstance(s, dict) and s.get("incomplete") is None]

        if not complete_snapshots:
            logger.warning("No complete snapshots found on %s", hostname)
            return None

        # Find the most recent startTime
        start_times = []
        for snapshot in complete_snapshots:
            start_time_str = snapshot.get("startTime")
            if start_time_str:
                try:
                    # Parse ISO8601 timestamp (Kopia uses RFC3339)
                    dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
                    start_times.append(dt)
                except (ValueError, AttributeError) as e:
                    logger.warning("Failed to parse timestamp '%s': %s", start_time_str, e)

        if not start_times:
            logger.warning("No valid timestamps found on %s", hostname)
            return None

        last_snapshot = max(start_times)
        logger.info("Last snapshot on %s: %s", hostname, last_snapshot.isoformat())
        return last_snapshot

    except json.JSONDecodeError as e:
        logger.error("Failed to parse JSON from %s: %s", hostname, e)
        logger.debug("Raw output: %s", result.stdout[:500])
        return None
    except Exception as e:
        logger.exception("Unexpected error parsing snapshots from %s: %s", hostname, e)
        return None


BACKUP_ASSESSMENT_PROMPT = """Analyze these Kopia backup service logs and determine the backup health status.

## Context
You're analyzing journalctl logs from a Kopia backup service. Your job is to determine if the backup is healthy.

## Ignorable Issues (still mark as SUCCESS)
- Docker overlay2 errors: 'unknown or unsupported entry type' - Docker filesystem artifacts
- ClickHouse partition deletion: 'no such file or directory' in clickhouse paths - Database MERGE operations
- Temporary file deletions: Files in /tmp, /var/tmp, /run deleted during backup
- Socket/FIFO files: Cannot be backed up, safe to ignore
- Minor warnings that don't affect backup integrity

## Warning Signs (mark as WARNING)
- High number of skipped files (but backup completed)
- Slow transfer speeds
- Retry attempts that eventually succeeded

## Critical Failures (mark as CRITICAL_FAILURE)
- Permission denied errors on critical paths (/home, /etc, /root, /var/lib/docker/data)
- Repository connection failures
- Disk full errors
- Backup service failed to start or crashed
- No successful snapshot created

## Logs to analyze:
```
{logs}
```

Analyze these logs and provide your assessment."""


async def fetch_ai_assessment(
    hostname: str,
    user: str,
    timeout: int = 30,
    port: int = 22,
    bastion_host: str | None = None,
) -> AIAssessment | None:
    """
    Analyze backup logs using AI to determine health status.

    Fetches journalctl logs and uses gpt-5-mini with structured outputs
    to intelligently assess backup health - no brittle regex parsing.
    """
    # Get API key
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_BENCH_OPENAI_API_KEY")
    if not api_key:
        logger.warning("No OpenAI API key configured - skipping AI assessment")
        return None

    # Fetch logs via SSH (only last 48h to avoid stale entries from old boots)
    command = "sudo journalctl -u kopia-backup.service --since '48 hours ago' -n 100 --no-pager"
    logger.debug("Fetching journalctl logs from %s", hostname)

    result = await asyncio.to_thread(
        run_ssh_command,
        hostname,
        command,
        user=user,
        port=port,
        timeout=timeout,
        bastion_host=bastion_host,
    )

    if not result.success:
        logger.warning("Failed to fetch journalctl from %s: %s", hostname, result.stderr[:200])
        return None

    logs = result.stdout.strip()
    if not logs or logs == "-- No entries --":
        logger.info("No backup logs found on %s (may have rebooted recently)", hostname)
        return None

    # Truncate logs if too long (keep last ~4000 chars to stay well under token limits)
    if len(logs) > 4000:
        logs = "... (truncated) ...\n" + logs[-4000:]

    # Call OpenAI with structured outputs
    try:
        client = OpenAI(api_key=api_key)

        response = await asyncio.to_thread(
            lambda: client.responses.parse(
                model=OPENAI_MODEL,
                input=[
                    {
                        "role": "user",
                        "content": BACKUP_ASSESSMENT_PROMPT.format(logs=logs),
                    }
                ],
                text_format=BackupAssessmentResponse,
            )
        )

        assessment = response.output_parsed
        if not assessment:
            logger.warning("OpenAI returned no parsed output for %s", hostname)
            return None

        logger.info(
            "AI Assessment on %s: status=%s, severity=%s, alert=%s",
            hostname,
            assessment.status,
            assessment.severity,
            assessment.alert,
        )
        logger.debug("AI Summary on %s: %s", hostname, assessment.summary[:100])

        return AIAssessment(
            status=assessment.status,
            severity=assessment.severity,
            alert=assessment.alert,
            summary=assessment.summary,
        )

    except Exception as e:
        logger.warning("OpenAI analysis failed for %s: %s", hostname, e)
        return None


# --- Age Calculation ---


def calculate_age_hours(snapshot_time: datetime) -> float:
    """Calculate the age of a snapshot in hours (UTC-aware)."""
    now = datetime.now(UTC)
    if snapshot_time.tzinfo is None:
        logger.warning("Snapshot time is not timezone-aware, assuming UTC")
        snapshot_time = snapshot_time.replace(tzinfo=UTC)

    age = now - snapshot_time
    return age.total_seconds() / 3600


# --- Host Status Check ---


async def check_host_status(host_config: dict[str, Any]) -> HostBackupStatus:
    """Check backup status for a single host."""
    host_id = host_config["id"]
    hostname = host_config["hostname"]
    user = host_config["user"]
    port = host_config.get("port", 22)
    path = host_config["path"]
    threshold_hours = host_config["threshold_hours"]
    bastion_host = host_config.get("bastion_host")
    kopia_path = host_config.get("kopia_path", "kopia")
    kopia_password_file = host_config.get("kopia_password_file")
    skip_ai_assessment = host_config.get("skip_ai_assessment", False)

    logger.info("Checking backup status for %s (%s)", host_id, hostname)

    status = HostBackupStatus(
        host_id=host_id,
        hostname=hostname,
        reachable=True,
        threshold_hours=threshold_hours,
    )

    # Test connectivity
    test_result = await asyncio.to_thread(
        run_ssh_command,
        hostname,
        "echo ok",
        user=user,
        port=port,
        timeout=10,
        bastion_host=bastion_host,
    )

    if not test_result.success:
        logger.error("Host %s unreachable: %s", host_id, test_result.stderr[:100])
        status.reachable = False
        status.error_message = f"SSH connection failed: {test_result.stderr[:200]}"
        return status

    # Fetch last snapshot time
    try:
        last_snapshot = await fetch_last_snapshot_time(
            hostname,
            user,
            path,
            port=port,
            bastion_host=bastion_host,
            kopia_path=kopia_path,
            kopia_password_file=kopia_password_file,
        )
        status.last_snapshot = last_snapshot

        if last_snapshot:
            age_hours = calculate_age_hours(last_snapshot)
            status.age_hours = age_hours
            status.is_stale = age_hours > threshold_hours
            logger.info(
                "%s: Last backup %.1fh ago (threshold: %dh, stale: %s)",
                host_id,
                age_hours,
                threshold_hours,
                status.is_stale,
            )
        else:
            logger.warning("%s: Could not determine last snapshot time", host_id)
            status.is_stale = True
            status.error_message = "Failed to fetch snapshot timestamp"

    except Exception as e:
        logger.exception("Error fetching snapshot time for %s: %s", host_id, e)
        status.error_message = f"Snapshot fetch error: {str(e)[:200]}"
        status.is_stale = True

    # Fetch AI assessment (skip for hosts without journalctl, e.g., macOS)
    if skip_ai_assessment:
        logger.info("%s: Skipping AI assessment (configured)", host_id)
    else:
        try:
            ai_assessment = await fetch_ai_assessment(hostname, user, port=port, bastion_host=bastion_host)
            status.ai_assessment = ai_assessment

            if not ai_assessment:
                logger.warning("%s: No AI assessment found in logs", host_id)

        except Exception as e:
            logger.exception("Error fetching AI assessment for %s: %s", host_id, e)
            # AI assessment is optional, don't fail the check

    logger.info("%s: Overall status = %s", host_id, status.overall_status)
    return status


# --- Email Formatting ---


def format_email_body(statuses: list[HostBackupStatus]) -> tuple[str, str]:
    """Format backup status report as both plain text and HTML."""
    # Count statuses
    success_count = sum(1 for s in statuses if s.overall_status == "success")
    warning_count = sum(1 for s in statuses if s.overall_status == "warning")
    critical_count = sum(1 for s in statuses if s.overall_status == "critical")

    # Plain text version
    plain_lines = []
    plain_lines.append("BACKUP SENTINEL REPORT")
    plain_lines.append("=" * 50)
    plain_lines.append(f"Success: {success_count}")
    plain_lines.append(f"Warning: {warning_count}")
    plain_lines.append(f"Critical: {critical_count}")
    plain_lines.append("")

    # HTML version
    html_lines = []
    html_lines.append("<html><body style='font-family: sans-serif;'>")
    html_lines.append("<h2>Backup Sentinel Report</h2>")
    html_lines.append(f"<p><strong>Success:</strong> {success_count} &nbsp;&nbsp; ")
    html_lines.append(f"<strong>Warning:</strong> {warning_count} &nbsp;&nbsp; ")
    html_lines.append(f"<strong>Critical:</strong> {critical_count}</p>")

    # Sort hosts: critical first, then warning, then success
    sorted_statuses = sorted(
        statuses,
        key=lambda s: (
            {"critical": 0, "warning": 1, "success": 2}.get(s.overall_status, 3),
            s.host_id,
        ),
    )

    # Host details
    plain_lines.append("HOST DETAILS")
    plain_lines.append("-" * 50)
    html_lines.append("<h3>Host Details</h3>")

    for status in sorted_statuses:
        # Status badge
        status_emoji = {
            "success": "[OK]",
            "warning": "[WARN]",
            "critical": "[CRIT]",
        }.get(status.overall_status, "?")

        status_color = {
            "success": "#28a745",
            "warning": "#ffc107",
            "critical": "#dc3545",
        }.get(status.overall_status, "#6c757d")

        # Plain text section
        plain_lines.append(f"\n{status_emoji} {status.host_id.upper()} ({status.hostname})")
        plain_lines.append(f"  Status: {status.overall_status}")

        # HTML section
        html_lines.append(f"<div style='border-left: 4px solid {status_color}; padding-left: 12px; margin-bottom: 16px;'>")
        html_lines.append(f"<h4 style='margin: 8px 0;'>{status_emoji} {status.host_id.upper()} ({status.hostname})</h4>")
        html_lines.append(
            f"<p style='margin: 4px 0;'><strong>Status:</strong> <span style='color: {status_color};'>{status.overall_status.upper()}</span></p>"
        )

        if not status.reachable:
            plain_lines.append("  OFFLINE - SSH connection failed")
            html_lines.append("<p style='margin: 4px 0; color: #dc3545;'><strong>OFFLINE</strong> - SSH connection failed</p>")
            if status.error_message:
                plain_lines.append(f"  Error: {status.error_message}")
                html_lines.append(f"<p style='margin: 4px 0; font-size: 0.9em; color: #666;'>Error: {status.error_message}</p>")
        else:
            if status.last_snapshot:
                age_str = f"{status.age_hours:.1f}h ago" if status.age_hours else "unknown"
                threshold_str = f"{status.threshold_hours}h"
                stale_marker = " [STALE]" if status.is_stale else ""

                plain_lines.append(f"  Last backup: {age_str} (threshold: {threshold_str}){stale_marker}")
                plain_lines.append(f"  Timestamp: {status.last_snapshot.strftime('%Y-%m-%d %H:%M:%S UTC')}")

                age_color = "#dc3545" if status.is_stale else "#28a745"
                html_lines.append(
                    f"<p style='margin: 4px 0;'><strong>Last backup:</strong> <span style='color: {age_color};'>{age_str}</span> (threshold: {threshold_str}){stale_marker}</p>"
                )
                html_lines.append(
                    f"<p style='margin: 4px 0; font-size: 0.9em; color: #666;'>Timestamp: {status.last_snapshot.strftime('%Y-%m-%d %H:%M:%S UTC')}</p>"
                )
            else:
                plain_lines.append("  Last backup: UNKNOWN")
                html_lines.append("<p style='margin: 4px 0; color: #dc3545;'><strong>Last backup:</strong> UNKNOWN</p>")

            # AI Assessment (only show if available)
            if status.ai_assessment:
                ai = status.ai_assessment
                ai_color = {
                    "success": "#28a745",
                    "warning": "#ffc107",
                    "critical_failure": "#dc3545",
                }.get(ai.status, "#6c757d")

                plain_lines.append(f"  AI Status: {ai.status} (severity: {ai.severity}/10)")
                html_lines.append(
                    f"<p style='margin: 4px 0;'><strong>AI Status:</strong> <span style='color: {ai_color};'>{ai.status}</span> (severity: {ai.severity}/10)</p>"
                )

                if ai.summary:
                    plain_lines.append(f"  AI Summary: {ai.summary[:200]}")
                    html_lines.append(f"<p style='margin: 4px 0; font-size: 0.9em; color: #666;'>Summary: {ai.summary[:200]}</p>")

            if status.error_message:
                plain_lines.append(f"  Error: {status.error_message}")
                html_lines.append(f"<p style='margin: 4px 0; font-size: 0.9em; color: #666;'>Error: {status.error_message}</p>")

        html_lines.append("</div>")

    html_lines.append("</body></html>")

    return "\n".join(plain_lines), "".join(html_lines)


# --- Main Entry Point ---


async def run() -> dict[str, Any]:
    """
    Run backup sentinel check for all configured hosts and send email report.

    This is the async entry point called by the Zerg scheduler.
    """
    logger.info("Running backup-sentinel job...")

    # Warm up Tailscale paths before SSH checks (prevents cold-connection timeouts)
    await warm_up_tailscale_paths()

    hosts = load_backup_config()["hosts"]
    logger.info("Checking %d hosts: %s", len(hosts), [h["id"] for h in hosts])

    # Check all hosts in parallel
    tasks = [check_host_status(host) for host in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to error statuses
    statuses = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            host_config = hosts[i]
            logger.exception("Unexpected error checking %s: %s", host_config["id"], result)
            statuses.append(
                HostBackupStatus(
                    host_id=host_config["id"],
                    hostname=host_config["hostname"],
                    reachable=False,
                    threshold_hours=host_config["threshold_hours"],
                    error_message=f"Unexpected error: {str(result)[:200]}",
                )
            )
        else:
            statuses.append(result)

    # Summary log
    success_count = sum(1 for s in statuses if s.overall_status == "success")
    warning_count = sum(1 for s in statuses if s.overall_status == "warning")
    critical_count = sum(1 for s in statuses if s.overall_status == "critical")

    logger.info(
        "Backup sentinel completed: %d success, %d warning, %d critical",
        success_count,
        warning_count,
        critical_count,
    )

    # Format email
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    plain_text, html = format_email_body(statuses)

    # Build metadata for reply tracking
    failed_hosts = [s.host_id for s in statuses if s.overall_status != "success"]
    alert_metadata = {"failed_hosts": failed_hosts, "date": date_str}

    # Determine severity and subject
    message_id = None
    if critical_count > 0:
        subject = f"[CRITICAL] Backup Sentinel - {success_count}/{len(statuses)} OK - {date_str}"
        message_id = await asyncio.to_thread(
            send_alert_email,
            subject,
            plain_text,
            level="CRITICAL",
            html=html,
            alert_type="backup",
            job_id="backup-sentinel",
            metadata=alert_metadata,
        )
    elif warning_count > 0:
        subject = f"[WARNING] Backup Sentinel - {success_count}/{len(statuses)} OK - {date_str}"
        message_id = await asyncio.to_thread(
            send_alert_email,
            subject,
            plain_text,
            level="WARNING",
            html=html,
            alert_type="backup",
            job_id="backup-sentinel",
            metadata=alert_metadata,
        )
    else:
        # All OK - just log, no email needed
        logger.info("All backups healthy - no email needed (%d/%d OK)", success_count, len(statuses))
        return {
            "success": success_count,
            "warning": warning_count,
            "critical": critical_count,
        }

    if message_id:
        logger.info("Alert email sent: %s", subject)
    else:
        logger.error("Failed to send alert email")
        logger.info("Report body:\n%s", plain_text)
        raise RuntimeError("Failed to send backup sentinel email")

    logger.info("Backup sentinel completed successfully")

    return {
        "hosts_total": len(statuses),
        "success": success_count,
        "warning": warning_count,
        "critical": critical_count,
    }


# --- Job Registration ---

# Register with job registry on module import
job_registry.register(
    JobConfig(
        id="backup-sentinel",
        cron="0 10 * * *",  # Daily at 10:00 UTC
        func=run,
        timeout_seconds=300,  # 5 minutes (matches Sauron)
        max_attempts=3,  # Match Sauron's retry policy
        tags=["backup", "monitoring", "critical"],
        project="infrastructure",
        description="Monitor Kopia backup freshness and health across all hosts",
    )
)
