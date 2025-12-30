import json
import time
from typing import Dict

import pytest
import requests

# Note: pytest_addoption is defined in root conftest.py


@pytest.fixture(scope="session")
def base_url(request):
    return request.config.getoption("--live-url")


@pytest.fixture(scope="session")
def auth_headers(request):
    token = request.config.getoption("--live-token")
    if not token:
        pytest.skip("Live tests require --live-token flag")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    return headers


class SupervisorClient:
    def __init__(self, base_url: str, headers: Dict[str, str]):
        self.base_url = base_url
        self.headers = headers

    def dispatch(self, task: str) -> int:
        """Dispatches a task and returns the run_id."""
        resp = requests.post(
            f"{self.base_url}/api/jarvis/supervisor", json={"task": task}, headers=self.headers, timeout=10
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            raise RuntimeError(f"Failed to dispatch task: {resp.status_code} {resp.text}")
        return resp.json()["run_id"]

    def wait_for_completion(self, run_id: int, timeout: int = 60) -> str:
        """Waits for completion and returns the final message."""
        return self._stream_and_parse(run_id, timeout)

    def collect_events(self, run_id: int, timeout: int = 60) -> list:
        """Collects all SSE events for a run and returns them as a list.

        Each event has the structure:
        {"type": "event_type", "data": {"type": "...", "payload": {...}, "seq": N, "timestamp": "..."}}
        """
        start_time = time.time()
        event_type = None
        events = []

        with requests.get(
            f"{self.base_url}/api/jarvis/supervisor/events",
            params={"run_id": run_id},
            headers=self.headers,
            stream=True,
            timeout=timeout,
        ) as response:
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError:
                raise RuntimeError(f"Failed to connect to SSE stream: {response.status_code} {response.text}")

            for line in response.iter_lines(decode_unicode=True):
                if time.time() - start_time > timeout:
                    raise TimeoutError("Timeout")

                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = data_str

                    # Collect event - store the complete SSE event wrapper
                    if event_type and isinstance(data, dict):
                        events.append({"type": event_type, "data": data})

                    # Check for completion
                    if event_type == "supervisor_complete":
                        return events

                    if event_type == "error":
                        # Extract error message from payload
                        error_msg = data
                        if isinstance(data, dict):
                            error_msg = data.get("payload", {}).get("error") or data.get("error", str(data))
                        raise RuntimeError(f"Supervisor Error: {error_msg}")

        return events

    def _stream_and_parse(self, run_id: int, timeout: int) -> str:
        """Streams SSE events and returns the final result message."""
        start_time = time.time()
        event_type = None

        with requests.get(
            f"{self.base_url}/api/jarvis/supervisor/events",
            params={"run_id": run_id},
            headers=self.headers,
            stream=True,
            timeout=timeout,
        ) as response:
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError:
                raise RuntimeError(f"Failed to connect to SSE stream: {response.status_code} {response.text}")

            for line in response.iter_lines(decode_unicode=True):
                if time.time() - start_time > timeout:
                    raise TimeoutError("Timeout")

                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = data_str

                    # Handle Events - access nested payload
                    if event_type == "supervisor_complete":
                        if isinstance(data, dict):
                            # Result is nested in payload
                            return data.get("payload", {}).get("result", "")
                        return str(data)

                    if event_type == "error":
                        # Error might be in payload or at root level (legacy)
                        error_msg = data
                        if isinstance(data, dict):
                            error_msg = data.get("payload", {}).get("error") or data.get("error", str(data))
                        raise RuntimeError(f"Supervisor Error: {error_msg}")

        return ""


@pytest.fixture(scope="session")
def supervisor_client(base_url, auth_headers):
    return SupervisorClient(base_url, auth_headers)
