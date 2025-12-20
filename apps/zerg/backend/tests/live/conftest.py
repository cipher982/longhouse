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
        resp.raise_for_status()
        return resp.json()["run_id"]

    def wait_for_completion(self, run_id: int, timeout: int = 60) -> str:
        """Waits for completion and returns the final message."""
        start_time = time.time()
        final_message = ""

        with requests.get(
            f"{self.base_url}/api/jarvis/supervisor/events",
            params={"run_id": run_id},
            headers=self.headers,
            stream=True,
            timeout=timeout,
        ) as response:
            for line in response.iter_lines(decode_unicode=True):
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"Timed out waiting for run {run_id}")

                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # We rely on the event type being passed in the previous 'event:' line
                    # But simpler logic here: standard SSE format is event\ndata\n\n
                    # To keep this robust without a full parser state machine:
                    # We assume Zerg sends 'event: x' then 'data: y'.
                    # But `iter_lines` loses that context unless we track it.
                    pass

        # Since iter_lines makes state tracking hard for multi-line SSE, let's use a simpler generator
        # that mimics the script logic, but cleaner.
        return self._stream_and_parse(run_id, timeout)

    def _stream_and_parse(self, run_id: int, timeout: int) -> str:
        start_time = time.time()
        event_type = None

        with requests.get(
            f"{self.base_url}/api/jarvis/supervisor/events",
            params={"run_id": run_id},
            headers=self.headers,
            stream=True,
            timeout=timeout,
        ) as response:
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

                    # Handle Events
                    if event_type == "supervisor_complete":
                        if isinstance(data, dict):
                            return data.get("message") or data.get("payload", {}).get("message", "")
                        return str(data)

                    if event_type == "error":
                        raise RuntimeError(f"Supervisor Error: {data}")

        return ""


@pytest.fixture(scope="session")
def supervisor_client(base_url, auth_headers):
    return SupervisorClient(base_url, auth_headers)
