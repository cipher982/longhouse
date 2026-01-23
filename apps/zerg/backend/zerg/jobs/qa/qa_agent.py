"""QA Agent job entry point.

Collects system health data, runs AI analysis, and persists state.
Following the "hybrid determinism" pattern: deterministic data collection,
AI-powered analysis and anomaly detection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.jobs.qa import config

logger = logging.getLogger(__name__)


def _default_state() -> dict[str, Any]:
    """Return default QA state structure."""
    return {
        "version": config.STATE_VERSION,
        "baseline": {},
        "issues": {},
        "checks_passed": 0,
        "checks_total": 0,
        "alert_sent": False,
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def _send_alerts_for_chronic_issues(
    new_state: dict[str, Any],
    previous_state: dict[str, Any] | None,
) -> bool:
    """Send Discord alerts for newly chronic issues.

    Only alerts on issues that just became chronic (weren't chronic before).
    Returns True if any alert was sent.
    """
    from zerg.services.ops_discord import send_qa_alert

    previous_issues = (previous_state or {}).get("issues", {})
    current_issues = new_state.get("issues", {})

    alerts_sent = 0
    for fingerprint, issue in current_issues.items():
        # Only alert on open, chronic issues
        if issue.get("status") != "open" or not issue.get("chronic"):
            continue

        # Check if this issue was already chronic before
        prev_issue = previous_issues.get(fingerprint, {})
        was_chronic = prev_issue.get("chronic", False)

        if not was_chronic:
            # Newly chronic - send alert
            logger.info("Sending alert for newly chronic issue: %s", fingerprint)
            await send_qa_alert(issue)
            alerts_sent += 1

    if alerts_sent > 0:
        logger.info("Sent %d QA alert(s)", alerts_sent)

    return alerts_sent > 0


async def run() -> dict[str, Any]:
    """QA agent job - collect data, run agent, persist state.

    Returns metadata dict that gets persisted to ops.runs.metadata.
    """
    started_at = datetime.now(UTC)
    run_dir = Path(config.RUN_DIR)
    job_dir = Path(__file__).parent

    # 1. Setup run directory (clear any stale data from previous runs)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    # 2. Fetch previous QA state from last successful run
    previous_state = await _fetch_previous_qa_state()
    (run_dir / "previous_state.json").write_text(json.dumps(previous_state or {}, indent=2))
    logger.info("Loaded previous QA state: %d issues tracked", len((previous_state or {}).get("issues", {})))

    # 3. Collect system health data
    collect_result = await _run_collect_script(job_dir, run_dir)
    if not collect_result["success"]:
        logger.warning("Data collection had issues: %s", collect_result.get("error", "unknown"))

    # 4. Run Claude agent for analysis
    agent_result = await _run_agent_analysis(job_dir, run_dir)

    # 5. Parse agent output for new state
    # IMPORTANT: If agent fails OR parse fails, preserve previous state to avoid false "all clear"
    if agent_result.get("success"):
        new_state, parse_ok = _parse_agent_state(agent_result.get("stdout", ""))
        if not parse_ok:
            logger.warning("Agent output parse failed, preserving previous state")
            new_state = previous_state or _default_state()
            new_state["agent_error"] = "parse_failed"
            new_state["updated_at"] = datetime.now(UTC).isoformat()
    else:
        logger.warning("Agent analysis failed, preserving previous state")
        new_state = previous_state or _default_state()
        new_state["agent_error"] = agent_result.get("error") or agent_result.get("status", "unknown")
        new_state["updated_at"] = datetime.now(UTC).isoformat()

    # 6. Send Discord alerts for new chronic issues
    alert_sent = False
    if new_state.get("alert_sent") and agent_result.get("success"):
        alert_sent = await _send_alerts_for_chronic_issues(new_state, previous_state)

    # 7. Calculate summary
    ended_at = datetime.now(UTC)
    duration_ms = int((ended_at - started_at).total_seconds() * 1000)

    issues = new_state.get("issues", {})
    open_issues = [i for i in issues.values() if i.get("status") == "open"]
    chronic_issues = [i for i in open_issues if i.get("chronic")]

    return {
        "qa_state": new_state,
        "checks_passed": new_state.get("checks_passed", 0),
        "checks_total": new_state.get("checks_total", 0),
        "issues_found": len(open_issues),
        "chronic_issues": len(chronic_issues),
        "alert_sent": alert_sent,  # Actual send result, not agent's suggestion
        "collect_status": collect_result.get("status", "unknown"),
        "agent_status": agent_result.get("status", "unknown"),
        "duration_ms": duration_ms,
    }


async def _fetch_previous_qa_state() -> dict[str, Any] | None:
    """Fetch QA state from the most recent successful run.

    Queries ops.runs for the last zerg-qa job with success status.
    """
    from zerg.jobs.ops_db import get_pool
    from zerg.jobs.ops_db import is_job_queue_db_enabled

    if not is_job_queue_db_enabled():
        logger.warning("Job queue DB not enabled, cannot fetch previous state")
        return None

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT metadata->'qa_state' as qa_state
                FROM ops.runs
                WHERE job_id = 'zerg-qa'
                  AND status = 'success'
                  AND metadata->'qa_state' IS NOT NULL
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            if row and row["qa_state"]:
                qa_state = row["qa_state"]
                # asyncpg may return dict (already decoded) or str (needs parsing)
                if isinstance(qa_state, dict):
                    return qa_state
                elif isinstance(qa_state, str):
                    return json.loads(qa_state)
                else:
                    logger.warning("Unexpected qa_state type: %s", type(qa_state))
                    return None
    except Exception as e:
        logger.warning("Failed to fetch previous QA state: %s", e)

    return None


async def _run_collect_script(job_dir: Path, run_dir: Path) -> dict[str, Any]:
    """Run the collect.sh script for deterministic data collection."""
    collect_script = job_dir / "collect.sh"

    if not collect_script.exists():
        logger.error("collect.sh not found at %s", collect_script)
        return {"success": False, "status": "missing_script", "error": "collect.sh not found"}

    try:
        # Run collect.sh with timeout
        result = await asyncio.to_thread(
            subprocess.run,
            ["/bin/bash", str(collect_script)],
            cwd=str(run_dir),
            capture_output=True,
            timeout=120,  # 2 minutes for collection
            env={**os.environ, "RUN_DIR": str(run_dir)},
        )

        # Check collect.status file
        status_file = run_dir / "collect.status"
        status = "unknown"
        if status_file.exists():
            status = status_file.read_text().strip()

        if result.returncode == 0 and status == "ok":
            return {"success": True, "status": "ok"}
        else:
            return {
                "success": False,
                "status": status,
                "error": result.stderr.decode("utf-8", errors="replace")[:1000],
                "returncode": result.returncode,
            }

    except subprocess.TimeoutExpired:
        logger.error("collect.sh timed out after 120s")
        return {"success": False, "status": "timeout", "error": "Script timed out"}
    except Exception as e:
        logger.exception("collect.sh failed: %s", e)
        return {"success": False, "status": "error", "error": str(e)}


async def _run_agent_analysis(job_dir: Path, run_dir: Path) -> dict[str, Any]:
    """Run AI analysis via z.ai API (Anthropic SDK compatible).

    Uses GLM-4.7 via z.ai's Anthropic-compatible API for analysis.
    Previously tried Claude Code CLI but it has issues in containerized environments
    (returns empty output despite exit code 0). SDK approach is more reliable.
    """
    import anthropic

    prompt_file = job_dir / "prompt.md"

    if not prompt_file.exists():
        logger.error("prompt.md not found at %s", prompt_file)
        return {"success": False, "status": "missing_prompt", "error": "prompt.md not found"}

    # Check for API key
    if not config.ZAI_API_KEY:
        logger.error("ZAI_API_KEY not set - cannot run agent analysis")
        return {"success": False, "status": "missing_api_key", "error": "ZAI_API_KEY environment variable not set"}

    try:
        base_prompt = prompt_file.read_text()

        # Build complete prompt with embedded data files
        data_files = [
            "health.json",
            "system_health.json",
            "errors_1h.json",
            "errors_24h.json",
            "performance.json",
            "stuck_workers.json",
            "collect_summary.json",
            "previous_state.json",
        ]

        file_contents = []
        for filename in data_files:
            filepath = run_dir / filename
            if filepath.exists():
                try:
                    content = filepath.read_text()
                    file_contents.append(f"## {filename}\n```json\n{content}\n```")
                except Exception as e:
                    file_contents.append(f"## {filename}\nError reading: {e}")
            else:
                file_contents.append(f"## {filename}\nFile not found")

        # Combine base prompt with data
        full_prompt = f"{base_prompt}\n\n---\n\n# Collected Data\n\n" + "\n\n".join(file_contents)

        # Create Anthropic client configured for z.ai
        client = anthropic.Anthropic(
            base_url=config.ZAI_BASE_URL,
            api_key=config.ZAI_API_KEY,
        )

        # Run the analysis (in a thread to avoid blocking)
        def call_api():
            return client.messages.create(
                model=config.ZAI_MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": full_prompt}],
            )

        response = await asyncio.wait_for(
            asyncio.to_thread(call_api),
            timeout=config.AGENT_TIMEOUT_SECONDS,
        )

        # Extract text from response
        stdout = response.content[0].text if response.content else ""

        return {"success": True, "status": "ok", "stdout": stdout, "stderr": ""}

    except asyncio.TimeoutError:
        logger.error("z.ai API timed out after %ds", config.AGENT_TIMEOUT_SECONDS)
        return {"success": False, "status": "timeout", "error": "Agent timed out"}
    except anthropic.AuthenticationError as e:
        logger.error("z.ai API authentication failed: %s", e)
        return {"success": False, "status": "auth_error", "error": f"Authentication failed: {e}"}
    except anthropic.APIError as e:
        logger.error("z.ai API error: %s", e)
        return {"success": False, "status": "api_error", "error": str(e)}
    except Exception as e:
        logger.exception("z.ai API call failed: %s", e)
        return {"success": False, "status": "error", "error": str(e)}


def _parse_agent_state(stdout: str) -> tuple[dict[str, Any], bool]:
    """Parse JSON state from agent output.

    Expects the agent to output a JSON block with the new QA state.
    Returns (state, parse_ok) tuple. On failure, returns (default_state, False).
    """
    default_state = _default_state()

    if not stdout:
        logger.warning("Empty agent output")
        return default_state, False

    # Look for JSON block in output (agent should output ```json ... ```)
    json_match = re.search(r"```json\s*\n(.*?)\n```", stdout, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            if isinstance(parsed, dict) and "issues" in parsed:
                return {**default_state, **parsed}, True
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse agent JSON block: %s", e)

    # Try parsing the entire output as JSON
    try:
        parsed = json.loads(stdout.strip())
        if isinstance(parsed, dict) and "issues" in parsed:
            return {**default_state, **parsed}, True
    except json.JSONDecodeError:
        pass

    # Look for inline JSON object (more permissive regex for nested objects)
    json_obj_match = re.search(r'\{.*"version".*"issues".*\}', stdout, re.DOTALL)
    if json_obj_match:
        try:
            parsed = json.loads(json_obj_match.group(0))
            if isinstance(parsed, dict) and "issues" in parsed:
                return {**default_state, **parsed}, True
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse valid QA state from agent output")
    return default_state, False


async def collect_health_data() -> dict[str, Any]:
    """Collect health data directly via Python (alternative to collect.sh).

    Can be called directly for testing or when bash is not available.
    """
    import httpx

    from zerg.jobs.ops_db import get_pool
    from zerg.jobs.ops_db import is_job_queue_db_enabled

    data: dict[str, Any] = {
        "collected_at": datetime.now(UTC).isoformat(),
        "checks": {},
    }

    # 1. API health check
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{config.API_URL_INTERNAL}/health")
            data["checks"]["health"] = {
                "status": "ok" if resp.status_code == 200 else "error",
                "status_code": resp.status_code,
                "response": resp.json() if resp.status_code == 200 else None,
            }
    except Exception as e:
        data["checks"]["health"] = {"status": "error", "error": str(e)}

    # 2. Database queries for reliability metrics
    if is_job_queue_db_enabled():
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Failed runs in last hour
                failed_1h = await conn.fetchval(
                    """
                    SELECT count(*) FROM zerg.agent_runs
                    WHERE status = 'failed'
                      AND created_at > now() - interval '1 hour'
                    """
                )
                data["checks"]["failed_runs_1h"] = {"count": failed_1h}

                # Failed runs in last 24h
                failed_24h = await conn.fetchval(
                    """
                    SELECT count(*) FROM zerg.agent_runs
                    WHERE status = 'failed'
                      AND created_at > now() - interval '24 hours'
                    """
                )
                data["checks"]["failed_runs_24h"] = {"count": failed_24h}

                # Total runs in last hour (for error rate)
                total_1h = await conn.fetchval(
                    """
                    SELECT count(*) FROM zerg.agent_runs
                    WHERE created_at > now() - interval '1 hour'
                    """
                )
                data["checks"]["total_runs_1h"] = {"count": total_1h}

                # Stuck workers (running > 10 min)
                stuck = await conn.fetchval(
                    """
                    SELECT count(*) FROM zerg.worker_jobs
                    WHERE status = 'running'
                      AND started_at < now() - interval '10 minutes'
                    """
                )
                data["checks"]["stuck_workers"] = {"count": stuck}

                # P95 latency (last 24h)
                latencies = await conn.fetch(
                    """
                    SELECT duration_ms FROM zerg.agent_runs
                    WHERE duration_ms IS NOT NULL
                      AND created_at > now() - interval '24 hours'
                    ORDER BY duration_ms
                    """
                )
                if latencies:
                    durations = [r["duration_ms"] for r in latencies]
                    p50_idx = len(durations) // 2
                    p95_idx = int(len(durations) * 0.95)
                    data["checks"]["latency"] = {
                        "p50_ms": durations[p50_idx] if p50_idx < len(durations) else None,
                        "p95_ms": durations[p95_idx] if p95_idx < len(durations) else None,
                        "count": len(durations),
                    }

        except Exception as e:
            logger.warning("Database queries failed: %s", e)
            data["checks"]["db_error"] = {"error": str(e)}

    return data
