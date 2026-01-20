"""Disk health monitoring for cube server.

Monitors SATA drives via SMART data:
- CRC error counts (incremental changes)
- Temperature
- Power-on hours
- Overall SMART health status

Migrated from Sauron.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.shared.email import send_alert_email
from zerg.shared.ssh import run_ssh_command

logger = logging.getLogger(__name__)

# SSH configuration for cube
DOCKER_HOST_SSH = "172.29.0.1"  # Gateway from Docker container
CUBE_TAILSCALE_IP = "100.104.187.47"
CUBE_SSH_PORT = 2222
CUBE_BASTION = f"root@{DOCKER_HOST_SSH}"

# State directory for persisting CRC error counts between runs.
# NOTE: Changed from /tmp/sauron-state to /tmp/zerg-state during migration.
# If using a volume mount for persistence, update the deployment config to match.
# Can be overridden via ZERG_STATE_DIR environment variable.
STATE_DIR = Path(os.getenv("ZERG_STATE_DIR", "/tmp/zerg-state"))
STATE_FILE = STATE_DIR / "cube-disk-health.json"


@dataclass
class DriveStatus:
    """SMART status for a single drive."""

    device: str
    model: str
    crc_errors: int
    temperature: int
    power_hours: int
    health: str
    previous_crc: int | None = None

    @property
    def crc_increased(self) -> bool:
        """Check if CRC errors increased since last check."""
        if self.previous_crc is None:
            return False
        return self.crc_errors > self.previous_crc

    @property
    def crc_delta(self) -> int:
        """Number of new CRC errors since last check."""
        if self.previous_crc is None:
            return 0
        return max(0, self.crc_errors - self.previous_crc)

    @property
    def is_critical(self) -> bool:
        """Check if drive has critical issues."""
        return self.health != "PASSED" or self.crc_increased

    @property
    def is_warning(self) -> bool:
        """Check if drive has warnings (high temp)."""
        return self.temperature > 50


def load_previous_state() -> dict[str, int]:
    """Load previous CRC error counts from state file."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
        return data.get("crc_errors", {})
    except Exception as e:
        logger.warning("Failed to load state file: %s", e)
        return {}


def save_state(crc_errors: dict[str, int]) -> None:
    """Save current CRC error counts to state file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = {
            "crc_errors": crc_errors,
            "updated": datetime.now(UTC).isoformat(),
        }
        STATE_FILE.write_text(json.dumps(data, indent=2))
        logger.debug("Saved state to %s", STATE_FILE)
    except Exception as e:
        logger.warning("Failed to save state file: %s", e)


async def fetch_smart_data(device: str) -> DriveStatus | None:
    """
    Fetch SMART data for a drive via SSH.

    Args:
        device: Device name without /dev/ (e.g., 'sda')

    Returns:
        DriveStatus or None if failed
    """
    command = f"smartctl -a /dev/{device}"

    result = await asyncio.to_thread(
        run_ssh_command,
        CUBE_TAILSCALE_IP,
        command,
        user="root",
        port=CUBE_SSH_PORT,
        timeout=30,
        bastion_host=CUBE_BASTION,
    )

    if not result.success:
        logger.warning("Failed to get SMART data for %s: %s", device, result.stderr[:200])
        return None

    output = result.stdout

    # Parse model
    model_match = re.search(r"Device Model:\s+(.+)", output)
    model = model_match.group(1).strip() if model_match else "Unknown"

    # Parse CRC errors (RAW_VALUE is last column, after the `-` separator)
    crc_pat = r"UDMA_CRC_Error_Count\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+-\s+(\d+)"
    crc_match = re.search(crc_pat, output)
    crc_errors = int(crc_match.group(1)) if crc_match else 0

    # Parse temperature (RAW_VALUE may have extra info in parens)
    temp_pat = r"Temperature_Celsius\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+-\s+(\d+)"
    temp_match = re.search(temp_pat, output)
    temperature = int(temp_match.group(1)) if temp_match else 0

    # Parse power-on hours
    hours_pat = r"Power_On_Hours\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+-\s+(\d+)"
    hours_match = re.search(hours_pat, output)
    power_hours = int(hours_match.group(1)) if hours_match else 0

    # Parse health status
    health_match = re.search(r"SMART overall-health.*?:\s*(\w+)", output)
    health = health_match.group(1) if health_match else "UNKNOWN"

    return DriveStatus(
        device=device,
        model=model,
        crc_errors=crc_errors,
        temperature=temperature,
        power_hours=power_hours,
        health=health,
    )


def format_email_body(drives: list[DriveStatus], has_critical: bool, has_warning: bool) -> tuple[str, str]:
    """Format disk health report as plain text and HTML."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Plain text
    plain_lines = [
        "CUBE DISK HEALTH REPORT",
        "=" * 50,
        f"Timestamp: {now}",
        "",
    ]

    # HTML
    html_lines = [
        "<html><body style='font-family: sans-serif;'>",
        "<h2>Cube Disk Health Report</h2>",
        f"<p style='color: #666;'>Timestamp: {now}</p>",
    ]

    for drive in drives:
        # Determine status color
        if drive.is_critical:
            status_color = "#dc3545"
            status_text = "CRITICAL"
            status_emoji = "X"
        elif drive.is_warning:
            status_color = "#ffc107"
            status_text = "WARNING"
            status_emoji = "!"
        else:
            status_color = "#28a745"
            status_text = "OK"
            status_emoji = "OK"

        # CRC delta text
        if drive.previous_crc is not None:
            if drive.crc_increased:
                crc_text = f"{drive.crc_errors} (+{drive.crc_delta} NEW)"
            else:
                crc_text = f"{drive.crc_errors} (unchanged from {drive.previous_crc})"
        else:
            crc_text = f"{drive.crc_errors} (first check)"

        # Plain text section
        plain_lines.append(f"[{status_emoji}] /dev/{drive.device} - {drive.model}")
        plain_lines.append(f"    Health: {drive.health}")
        plain_lines.append(f"    Temperature: {drive.temperature}C")
        plain_lines.append(f"    Power-on Hours: {drive.power_hours:,}")
        plain_lines.append(f"    CRC Errors: {crc_text}")
        plain_lines.append("")

        # HTML section
        html_lines.append(f"<div style='border-left: 4px solid {status_color}; " f"padding-left: 12px; margin-bottom: 16px;'>")
        html_lines.append(f"<h4 style='margin: 8px 0;'>/dev/{drive.device} - {drive.model}</h4>")
        html_lines.append(
            f"<p style='margin: 4px 0;'><strong>Status:</strong> " f"<span style='color: {status_color};'>{status_text}</span></p>"
        )
        html_lines.append(f"<p style='margin: 4px 0;'><strong>Health:</strong> {drive.health}</p>")

        temp_color = "#dc3545" if drive.temperature > 50 else "#28a745"
        html_lines.append(
            f"<p style='margin: 4px 0;'><strong>Temperature:</strong> "
            f"<span style='color: {temp_color};'>{drive.temperature}C</span></p>"
        )
        html_lines.append(f"<p style='margin: 4px 0;'><strong>Power-on Hours:</strong> {drive.power_hours:,}</p>")

        crc_color = "#dc3545" if drive.crc_increased else "#28a745"
        html_lines.append(
            f"<p style='margin: 4px 0;'><strong>CRC Errors:</strong> " f"<span style='color: {crc_color};'>{crc_text}</span></p>"
        )
        html_lines.append("</div>")

    # Add troubleshooting section if critical
    if has_critical:
        plain_lines.append("-" * 50)
        plain_lines.append("RECOMMENDED ACTIONS:")
        plain_lines.append("1. SSH to cube: ssh cube")
        plain_lines.append("2. Check dmesg: sudo dmesg | grep -i 'ata\\|crc\\|error' | tail -50")
        plain_lines.append("3. Review logs: ls /var/log/disk-health/")
        plain_lines.append("4. If CRC errors increasing: Replace SATA cables or test different ports")

        html_lines.append("<hr style='margin: 20px 0;'>")
        html_lines.append("<h4 style='color: #dc3545;'>Recommended Actions</h4>")
        html_lines.append("<ol>")
        html_lines.append("<li>SSH to cube: <code>ssh cube</code></li>")
        html_lines.append("<li>Check dmesg: <code>sudo dmesg | grep -i 'ata|crc|error' | tail -50</code></li>")
        html_lines.append("<li>Review logs: <code>ls /var/log/disk-health/</code></li>")
        html_lines.append("<li>If CRC errors increasing: Replace SATA cables or test different ports</li>")
        html_lines.append("</ol>")

    html_lines.append("</body></html>")

    return "\n".join(plain_lines), "".join(html_lines)


async def run() -> dict[str, Any]:
    """
    Run disk health check for cube server.

    This is the async entry point called by the Zerg scheduler.
    """
    logger.info("Running disk-health-cube job...")

    # Load previous state
    previous_crc = load_previous_state()
    logger.info("Previous CRC counts: %s", previous_crc)

    # Devices to check
    devices = ["sda", "sdb"]

    # Fetch SMART data for all drives
    drives: list[DriveStatus] = []
    for device in devices:
        status = await fetch_smart_data(device)
        if status:
            # Attach previous CRC count
            status.previous_crc = previous_crc.get(device)
            drives.append(status)
            logger.info(
                "%s (%s): CRC=%d, Temp=%dC, Hours=%d, Health=%s",
                device,
                status.model,
                status.crc_errors,
                status.temperature,
                status.power_hours,
                status.health,
            )
        else:
            logger.warning("Failed to get SMART data for %s", device)

    if not drives:
        logger.error("No drives found or all SMART queries failed")
        raise RuntimeError("Failed to get SMART data from any drive")

    # Save current state for next run
    current_crc = {d.device: d.crc_errors for d in drives}
    save_state(current_crc)

    # Determine overall status
    has_critical = any(d.is_critical for d in drives)
    has_warning = any(d.is_warning for d in drives)

    # Format email
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    plain_text, html = format_email_body(drives, has_critical, has_warning)

    # Build alert metadata
    alert_metadata = {
        "host": "cube",
        "drives": [d.device for d in drives if d.is_critical or d.is_warning],
        "date": date_str,
    }

    # Send appropriate email
    message_id = None
    if has_critical:
        # Check if CRC errors increased specifically
        crc_increased = any(d.crc_increased for d in drives)
        if crc_increased:
            subject = f"Cube Disk Health - CRC Errors Increased - {date_str}"
            level = "CRITICAL"
        else:
            subject = f"Cube Disk Health - SMART Issue Detected - {date_str}"
            level = "WARNING"
        message_id = await asyncio.to_thread(
            send_alert_email,
            subject,
            plain_text,
            level=level,
            html=html,
            alert_type="disk",
            job_id="disk-health-cube",
            metadata=alert_metadata,
        )
    elif has_warning:
        subject = f"Cube Disk Health - High Temperature - {date_str}"
        message_id = await asyncio.to_thread(
            send_alert_email,
            subject,
            plain_text,
            level="WARNING",
            html=html,
            alert_type="disk",
            job_id="disk-health-cube",
            metadata=alert_metadata,
        )
    else:
        # All OK - just log, no email needed
        logger.info("All drives healthy - no email needed (drives: %d)", len(drives))
        return {"status": "ok", "drives": len(drives)}

    if message_id:
        logger.info("Alert email sent: %s", subject)
    else:
        logger.error("Failed to send alert email")
        logger.info("Report body:\n%s", plain_text)
        raise RuntimeError("Failed to send disk health email")

    logger.info("Disk health check completed successfully")

    return {
        "drives_checked": len(drives),
        "has_critical": has_critical,
        "has_warning": has_warning,
        "crc_errors": current_crc,
    }


# --- Job Registration ---

# Register with job registry on module import
job_registry.register(
    JobConfig(
        id="disk-health-cube",
        cron="0 12 * * *",  # Daily at 12:00 UTC (matches Sauron schedule)
        func=run,
        timeout_seconds=120,  # 2 minutes
        max_attempts=3,  # Match Sauron's retry policy
        tags=["monitoring", "disk", "hardware"],
        project="infrastructure",
        description="Monitor SMART health data for cube server SATA drives",
    )
)
