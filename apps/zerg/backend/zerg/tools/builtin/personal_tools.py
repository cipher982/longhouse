"""Personal tools for Jarvis integration (Phase 4 v2.1).

These tools enable Concierge to access personal data sources:
- Location: GPS position from Traccar tracking server
- Health: Recovery/sleep/strain from WHOOP
- Notes: Search Obsidian vault via Runner

All tools use the connector credential system for secure storage of
API tokens and configuration.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success
from zerg.utils.crypto import decrypt

logger = logging.getLogger(__name__)


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine from a sync context.

    Similar pattern to runner_tools.py - handles both cases where
    we're inside an event loop or not.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_commis=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


# ---------------------------------------------------------------------------
# Location Tool (Traccar)
# ---------------------------------------------------------------------------


def get_current_location(include_address: bool = True) -> Dict[str, Any]:
    """Get current GPS location from Traccar tracking server.

    Returns the latest position for the configured device, including
    coordinates and optionally reverse-geocoded address.

    Args:
        include_address: Whether to include human-readable address (default: True)

    Returns:
        Success envelope with:
        - lat: Latitude
        - lon: Longitude
        - address: Human-readable address (if available and requested)
        - speed: Current speed in knots
        - battery: Device battery percentage (if reported)
        - updated_at: Timestamp of last position update

        Or error envelope if:
        - Traccar credentials not configured
        - Device not found
        - API request failed

    Example:
        >>> get_current_location()
        {
            "ok": True,
            "data": {
                "lat": 37.7749,
                "lon": -122.4194,
                "address": "San Francisco, CA, USA",
                "speed": 0,
                "updated_at": "2025-01-15T10:30:00Z"
            }
        }
    """
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Location tool requires credential context",
        )

    creds = resolver.get(ConnectorType.TRACCAR)
    if not creds:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Traccar integration not configured. Please add Traccar credentials in Settings > Connectors.",
        )

    url = creds.get("url", "").rstrip("/")
    username = creds.get("username", "admin")
    password = creds.get("password", "")
    device_id = creds.get("device_id")

    if not url or not password:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Traccar credentials incomplete. Please configure URL and password.",
        )

    try:
        # Traccar uses Basic Auth (username:password)
        auth = (username, password)

        with httpx.Client(timeout=10.0) as client:
            # Get positions (latest for all devices or specific device)
            positions_url = f"{url}/api/positions"
            if device_id:
                positions_url += f"?deviceId={device_id}"

            response = client.get(positions_url, auth=auth)

            if response.status_code == 401:
                return tool_error(
                    ErrorType.VALIDATION_ERROR,
                    "Traccar authentication failed. Please check your username and password.",
                )

            if response.status_code != 200:
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    f"Traccar API returned status {response.status_code}",
                )

            positions = response.json()
            if not positions:
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    "No position data available. Device may be offline.",
                )

            # Get the latest position (or first if only one)
            pos = positions[0] if isinstance(positions, list) else positions

            result = {
                "lat": pos.get("latitude"),
                "lon": pos.get("longitude"),
                "speed": pos.get("speed", 0),
                "updated_at": pos.get("fixTime") or pos.get("serverTime"),
            }

            # Include address if available
            if include_address and pos.get("address"):
                result["address"] = pos["address"]

            # Include battery if available
            attrs = pos.get("attributes", {})
            if "batteryLevel" in attrs:
                result["battery"] = attrs["batteryLevel"]

            return tool_success(result)

    except httpx.TimeoutException:
        return tool_error(ErrorType.EXECUTION_ERROR, "Traccar request timed out")
    except httpx.RequestError as e:
        return tool_error(ErrorType.EXECUTION_ERROR, f"Traccar request failed: {e}")
    except Exception as e:
        logger.exception("Unexpected error in get_current_location")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Unexpected error: {e}")


# ---------------------------------------------------------------------------
# WHOOP Health Tool
# ---------------------------------------------------------------------------


def _refresh_whoop_token(refresh_token: str, client_id: str, client_secret: str, owner_id: int, db) -> Dict[str, Any] | None:
    """Refresh WHOOP access token using refresh token.

    Args:
        refresh_token: The refresh token from WHOOP OAuth
        client_id: OAuth client ID from user's WHOOP app registration
        client_secret: OAuth client secret from user's WHOOP app registration
        owner_id: User ID to update credentials for
        db: Database session

    Returns:
        New credentials dict with access_token and refresh_token, or None if failed
    """
    try:
        import json

        import httpx

        from zerg.models.models import AccountConnectorCredential
        from zerg.utils.crypto import encrypt

        logger.info(f"Refreshing WHOOP token for user {owner_id}")

        # Exchange refresh token for new access token
        response = httpx.post(
            "https://api.prod.whoop.com/oauth/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10.0,
        )

        if response.status_code != 200:
            logger.error(f"WHOOP token refresh failed: {response.status_code} {response.text}")
            return None

        token_data = response.json()

        # IMPORTANT: Preserve client_id/client_secret when updating tokens
        # Otherwise next refresh will fail due to missing OAuth app credentials
        credential = (
            db.query(AccountConnectorCredential)
            .filter(
                AccountConnectorCredential.owner_id == owner_id,
                AccountConnectorCredential.connector_type == "whoop",
            )
            .first()
        )

        if credential:
            # Decrypt existing credentials to preserve client_id/secret
            existing_creds = json.loads(decrypt(credential.encrypted_value))

            # Update tokens while preserving OAuth app credentials
            new_creds = {
                "client_id": existing_creds.get("client_id", client_id),
                "client_secret": existing_creds.get("client_secret", client_secret),
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", refresh_token),
            }

            credential.encrypted_value = encrypt(json.dumps(new_creds))
            credential.test_status = "untested"
            db.commit()
            logger.info(f"WHOOP token refreshed successfully for user {owner_id}")

            return new_creds

        # Shouldn't happen, but handle gracefully
        logger.error(f"WHOOP credential not found for user {owner_id} during refresh")
        return None

    except Exception:
        logger.exception(f"Failed to refresh WHOOP token for user {owner_id}")
        return None


def get_whoop_data(
    date: Optional[str] = None,
    include_sleep: bool = True,
    include_strain: bool = True,
) -> Dict[str, Any]:
    """Get health metrics from WHOOP fitness tracker.

    Returns recovery score, sleep data, and strain for the specified date
    (defaults to today).

    Args:
        date: Date in YYYY-MM-DD format (default: today)
        include_sleep: Include sleep duration and quality (default: True)
        include_strain: Include strain score (default: True)

    Returns:
        Success envelope with:
        - recovery_score: Recovery percentage (0-100)
        - hrv: Heart rate variability in ms
        - resting_hr: Resting heart rate in bpm
        - sleep_hours: Total sleep duration (if requested)
        - sleep_quality: Sleep performance percentage (if requested)
        - strain: Day strain score 0-21 (if requested)
        - date: The date of the data

        Or error envelope if:
        - WHOOP credentials not configured
        - API request failed
        - No data for the requested date

    Example:
        >>> get_whoop_data()
        {
            "ok": True,
            "data": {
                "recovery_score": 78,
                "hrv": 45,
                "resting_hr": 52,
                "sleep_hours": 7.5,
                "sleep_quality": 85,
                "strain": 12.4,
                "date": "2025-01-15"
            }
        }
    """
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "WHOOP tool requires credential context",
        )

    creds = resolver.get(ConnectorType.WHOOP)
    if not creds:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "WHOOP integration not configured. Please add WHOOP credentials in Settings > Connectors.",
        )

    access_token = creds.get("access_token", "")
    refresh_token = creds.get("refresh_token")
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")

    if not access_token:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "WHOOP access token not configured.",
        )

    # Refresh requires OAuth app credentials
    if refresh_token and (not client_id or not client_secret):
        logger.warning(f"WHOOP refresh_token present but client credentials missing for user {resolver.owner_id}")

    # Default to today
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Attempt API calls, with automatic token refresh on 401
    max_retries = 2  # Original attempt + 1 retry after refresh
    for attempt in range(max_retries):
        try:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            }

            result: Dict[str, Any] = {"date": date}

            with httpx.Client(timeout=15.0, base_url="https://api.prod.whoop.com") as client:
                # Get recovery data
                recovery_response = client.get(
                    "/developer/v1/recovery",
                    headers=headers,
                    params={"start": date, "end": date},
                )

                # Handle 401 - try to refresh token
                if recovery_response.status_code == 401:
                    if attempt < max_retries - 1 and refresh_token and client_id and client_secret:
                        logger.info("WHOOP token expired, attempting refresh...")
                        # Get database session from resolver
                        from zerg.database import get_db

                        db = next(get_db())
                        try:
                            new_creds = _refresh_whoop_token(refresh_token, client_id, client_secret, resolver.owner_id, db)
                            if new_creds:
                                # Update token for retry
                                access_token = new_creds["access_token"]
                                refresh_token = new_creds.get("refresh_token")
                                continue  # Retry with new token
                        finally:
                            db.close()

                    return tool_error(
                        ErrorType.VALIDATION_ERROR,
                        "WHOOP authentication failed. Please re-authorize the app.",
                    )

                if recovery_response.status_code == 200:
                    recovery_data = recovery_response.json()
                    records = recovery_data.get("records", [])
                    if records:
                        rec = records[0]
                        score = rec.get("score", {})
                        result["recovery_score"] = score.get("recovery_score")
                        result["hrv"] = score.get("hrv_rmssd_milli")
                        result["resting_hr"] = score.get("resting_heart_rate")

                # Get sleep data if requested
                if include_sleep:
                    sleep_response = client.get(
                        "/developer/v1/activity/sleep",
                        headers=headers,
                        params={"start": date, "end": date},
                    )

                    if sleep_response.status_code == 200:
                        sleep_data = sleep_response.json()
                        records = sleep_data.get("records", [])
                        if records:
                            sleep = records[0]
                            score = sleep.get("score", {})
                            # Convert milliseconds to hours
                            total_ms = score.get("total_in_bed_time_milli", 0)
                            if total_ms:
                                result["sleep_hours"] = round(total_ms / 3600000, 1)
                            result["sleep_quality"] = score.get("sleep_performance_percentage")

                # Get strain data if requested
                if include_strain:
                    strain_response = client.get(
                        "/developer/v1/cycle",
                        headers=headers,
                        params={"start": date, "end": date},
                    )

                    if strain_response.status_code == 200:
                        strain_data = strain_response.json()
                        records = strain_data.get("records", [])
                        if records:
                            cycle = records[0]
                            score = cycle.get("score", {})
                            result["strain"] = score.get("strain")

            # Check if we got any data
            if len(result) == 1:  # Only has 'date'
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    f"No WHOOP data available for {date}",
                )

            return tool_success(result)

        except httpx.TimeoutException:
            return tool_error(ErrorType.EXECUTION_ERROR, "WHOOP API request timed out")
        except httpx.RequestError as e:
            return tool_error(ErrorType.EXECUTION_ERROR, f"WHOOP API request failed: {e}")
        except Exception as e:
            logger.exception("Unexpected error in get_whoop_data")
            return tool_error(ErrorType.EXECUTION_ERROR, f"Unexpected error: {e}")

    # If we get here, all retries failed
    return tool_error(ErrorType.EXECUTION_ERROR, "WHOOP API request failed after retries")


# ---------------------------------------------------------------------------
# Obsidian Notes Search Tool (Runner-backed)
# ---------------------------------------------------------------------------


def search_notes(
    query: str,
    limit: int = 5,
) -> Dict[str, Any]:
    """Search personal notes in Obsidian vault via Runner.

    Uses ripgrep on the user's Runner (laptop/server where vault is synced)
    to search note contents. Returns matching notes with context.

    Args:
        query: Search query (supports basic text matching)
        limit: Maximum number of results to return (default: 5)

    Returns:
        Success envelope with:
        - results: List of matching notes with:
            - path: File path relative to vault
            - title: Note title (filename without .md)
            - matches: List of matching lines with context
        - total_matches: Total number of matches found

        Or error envelope if:
        - Obsidian credentials not configured
        - Runner not available
        - Search failed

    Example:
        >>> search_notes("project ideas")
        {
            "ok": True,
            "data": {
                "results": [
                    {
                        "path": "Projects/Ideas.md",
                        "title": "Ideas",
                        "matches": ["- New project idea for automation"]
                    }
                ],
                "total_matches": 1
            }
        }
    """
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Notes search requires credential context",
        )

    creds = resolver.get(ConnectorType.OBSIDIAN)
    if not creds:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Obsidian integration not configured. Please add Obsidian settings in Settings > Connectors.",
        )

    vault_path = creds.get("vault_path", "")
    runner_name = creds.get("runner_name", "")

    if not vault_path or not runner_name:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Obsidian configuration incomplete. Please set vault_path and runner_name.",
        )

    # Sanitize query to prevent command injection
    # Allow alphanumeric, spaces, and common punctuation
    safe_query = "".join(c for c in query if c.isalnum() or c in " -_.'\"")
    if not safe_query:
        return tool_error(ErrorType.VALIDATION_ERROR, "Invalid search query")

    # Expand ~ in vault path for shell execution
    # Note: Runner executes in user's shell, so ~ expands on runner side
    expanded_vault = vault_path.replace("~", "$HOME")

    # Build ripgrep command
    # -i: case insensitive
    # -C 1: 1 line of context
    # --max-count: limit matches per file
    # --type md: only markdown files
    rg_command = f'rg -i -C 1 --max-count 3 --type md "{safe_query}" {expanded_vault} 2>/dev/null | head -n 100'

    try:
        # Execute via runner using concierge-safe method
        # runner_exec requires commis context, so we use the dispatcher directly
        from zerg.crud import runner_crud
        from zerg.database import get_db
        from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher

        db = next(get_db())
        try:
            # Resolve runner by name
            runner = runner_crud.get_runner_by_name(db, resolver.owner_id, runner_name)
            if not runner:
                return tool_error(
                    ErrorType.VALIDATION_ERROR,
                    f"Runner '{runner_name}' not found. Is it enrolled and online?",
                )

            if runner.status == "offline":
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    f"Runner '{runner_name}' is offline. Please start the Runner.",
                )

            if runner.status == "revoked":
                return tool_error(
                    ErrorType.VALIDATION_ERROR,
                    f"Runner '{runner_name}' has been revoked.",
                )

            # Dispatch job to runner
            dispatcher = get_runner_job_dispatcher()
            result = _run_coro_sync(
                dispatcher.dispatch_job(
                    db=db,
                    owner_id=resolver.owner_id,
                    runner_id=runner.id,
                    command=rg_command,
                    timeout_secs=30,
                    commis_id=None,  # No commis context for Concierge tools
                    course_id=None,
                )
            )
        finally:
            db.close()

        if not result.get("ok"):
            error = result.get("error", {})
            error_msg = error.get("message", "Unknown error")

            # Check for common issues
            if "not found" in error_msg.lower():
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    f"Runner '{runner_name}' not found. Is your laptop Runner online?",
                )
            if "offline" in error_msg.lower():
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    f"Runner '{runner_name}' is offline. Please start the Runner on your laptop.",
                )

            return tool_error(ErrorType.EXECUTION_ERROR, error_msg)

        data = result.get("data", {})
        stdout = data.get("stdout", "")
        exit_code = data.get("exit_code", 0)

        # ripgrep returns exit code 1 if no matches (not an error)
        if exit_code == 1 and not stdout:
            return tool_success(
                {
                    "results": [],
                    "total_matches": 0,
                    "message": f"No notes found matching '{query}'",
                }
            )

        # Parse ripgrep output
        results = _parse_ripgrep_output(stdout, vault_path, limit)

        return tool_success(
            {
                "results": results,
                "total_matches": len(results),
            }
        )

    except Exception as e:
        logger.exception("Unexpected error in search_notes")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Search failed: {e}")


def _parse_ripgrep_output(output: str, vault_path: str, limit: int) -> List[Dict[str, Any]]:
    """Parse ripgrep output into structured results.

    Ripgrep output format with -C 1:
    path/to/file.md-context before
    path/to/file.md:matching line
    path/to/file.md-context after
    --
    """
    results: Dict[str, Dict[str, Any]] = {}

    for line in output.split("\n"):
        if line == "--" or not line:
            continue

        # Parse file:line or file-line format
        if ":" in line or "-" in line:
            # Find the separator (: for match, - for context)
            sep_idx = -1
            for i, c in enumerate(line):
                if c in ":-" and i > 0:
                    # Check if this looks like a file path ending
                    potential_path = line[:i]
                    if potential_path.endswith(".md"):
                        sep_idx = i
                        break

            if sep_idx > 0:
                file_path = line[:sep_idx]
                content = line[sep_idx + 1 :].strip()

                # Make path relative to vault
                if file_path.startswith(vault_path):
                    file_path = file_path[len(vault_path) :].lstrip("/")

                if file_path not in results:
                    if len(results) >= limit:
                        continue
                    # Extract title from filename
                    title = file_path.rsplit("/", 1)[-1]
                    if title.endswith(".md"):
                        title = title[:-3]

                    results[file_path] = {
                        "path": file_path,
                        "title": title,
                        "matches": [],
                    }

                # Only add non-empty content lines
                if content and len(results[file_path]["matches"]) < 3:
                    results[file_path]["matches"].append(content)

    return list(results.values())


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=get_current_location,
        name="get_current_location",
        description=(
            "Get current GPS location from Traccar tracking server. "
            "Returns latitude, longitude, address, and last update time. "
            "Use this when the user asks about their location or whereabouts."
        ),
    ),
    StructuredTool.from_function(
        func=get_whoop_data,
        name="get_whoop_data",
        description=(
            "Get health metrics from WHOOP fitness tracker including recovery score, "
            "HRV, resting heart rate, sleep duration/quality, and strain. "
            "Use this when the user asks about their health, recovery, sleep, or fitness data."
        ),
    ),
    StructuredTool.from_function(
        func=search_notes,
        name="search_notes",
        description=(
            "Search personal notes in Obsidian vault. Searches note contents "
            "and returns matching files with context. Use this when the user "
            "asks to find or search their notes, knowledge base, or personal documentation."
        ),
    ),
]
