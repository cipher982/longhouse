"""What every producer that drives a live Longhouse does, once.

The producers each drive a real provider through a Runtime Host they own and
watch what Longhouse does with it. The small things all of them need for that
were written again in each, with small differences nobody chose: five waits,
five Runtime Host clients with five copies of one error, a dozen checks that a
process is gone. This module is the one copy:

- ``poll``: read until a value arrives or a deadline passes, and say what the
  wait was waiting on when it expires (``zerg.qa.wait_kind``).
- ``runtime_host_json`` and ``RuntimeHostError``: one request to the Runtime
  Host's agents API, and the one error a refused request raises.
- ``pid_dead``, ``group_dead``, ``pid_alive``: whether a recorded process is
  gone. A value that is not a pid is neither: a missing identity proves nothing.
- The Runtime Host's address, and the guard against pointing a qualification
  run at a host a person uses.

It imports nothing from the producers, so any of them can import it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa.wait_kind import expired_wait

RUNTIME_API_URL_ENV = "LONGHOUSE_RUNTIME_API_URL"
RUNTIME_AGENTS_TOKEN_ENV = "LONGHOUSE_RUNTIME_AGENTS_TOKEN"
HOSTED_TARGET_ENV = "LONGHOUSE_QA_HOSTED_TARGET"
RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"


def poll(
    read: Callable[[], Any],
    *,
    timeout: float,
    description: str | None = None,
    interval: float = 0.2,
    until: Callable[[Any], bool] = bool,
    waited_on: object = None,
    error: type[Exception] = RuntimeError,
    on_timeout: Callable[[str], Exception] | None = None,
) -> Any:
    """Call ``read`` until its value satisfies ``until`` (truthy, by default), or ``timeout`` seconds pass.

    A wait with a ``description`` raises when it expires: ``error`` with
    ``timed out waiting for <description>``, or what ``on_timeout`` builds
    from that prefix for a wait that can say what it last saw. ``waited_on``
    is what the wait was waiting on (``zerg.qa.wait_kind``), or a callable
    that decides it at expiry; a wait that cannot tell declares nothing.

    A wait without a description is a question, not a requirement: it takes
    one last reading at the deadline and returns it, for its caller to judge.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if until(value):
            return value
        time.sleep(interval)
    if description is None:
        return read()
    prefix = f"timed out waiting for {description}"
    raise expired_wait(on_timeout(prefix) if on_timeout is not None else error(prefix), waited_on)


def _is_pid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _exited_uncollected(pid: int) -> bool:
    """Whether the process has exited and only its entry is left for its parent to collect (a zombie)."""

    try:
        # The state follows the command name, which may itself hold spaces and brackets.
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()[:1] == ["Z"]
    except OSError:
        # No /proc entry to read: another platform, or a Linux container that has no /proc. Ask ps, on both.
        pass
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state.strip().startswith("Z")


def pid_dead(pid: object) -> bool:
    """Whether the process has stopped: none has this pid, or it has exited and not been collected yet.

    One rule for every producer. Collecting an exited child is its parent's
    duty and a separate question: a process that only awaits that runs
    nothing. A process this user may not signal exists.
    """

    if not _is_pid(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return _exited_uncollected(pid)


def group_dead(pgid: object) -> bool:
    """Whether no process is left in this process group.

    An exited member that has not been collected still holds its group: the
    owner of a child collects it before asking (``ProviderPtySession.close``).
    """

    if not _is_pid(pgid):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def pid_alive(pid: object) -> bool:
    return _is_pid(pid) and not pid_dead(pid)


class RuntimeHostError(RuntimeError):
    """The Runtime Host refused a request: its status, and the detail it gave."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        # What a wait that gives up on this error had last seen of the session, when it attaches it.
        self.last_runtime_observation: dict[str, Any] | None = None
        super().__init__(f"Runtime Host HTTP {status}: {detail}")


def runtime_host_json(
    api_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    json_body: Mapping[str, Any] | None = None,
    extra_headers: Mapping[str, str] | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    """One request to the Runtime Host's agents API (``path`` is below ``/api/agents/``), answered with an object.

    ``extra_headers`` is for a caller that authenticates with a session's
    coordination token and so also names the session it speaks as.
    ``Content-Type`` goes with a body and not without one: no agents-API
    handler this is called for reads it (the two media uploads do, and are
    not called through here).
    """

    headers = {"X-Agents-Token": token, "Accept": "application/json", "User-Agent": RUNTIME_HOST_USER_AGENT}
    headers.update(extra_headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""
    request = urllib.request.Request(f"{api_url.rstrip('/')}/api/agents/{path.lstrip('/')}", headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            raw_detail = exc.read(4096).decode("utf-8", "replace")
        except OSError:
            raw_detail = ""
        try:
            parsed_detail = json.loads(raw_detail)
        except json.JSONDecodeError:
            parsed_detail = raw_detail
        if isinstance(parsed_detail, dict):
            parsed_detail = parsed_detail.get("detail") or parsed_detail
        raise RuntimeHostError(exc.code, str(parsed_detail)[:1000]) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime Host returned a non-object")
    return payload


def require_disposable_runtime(api_url: str | None) -> None:
    """Refuse a live QA run against a non-loopback Runtime Host unless it is named.

    Qualification, canaries and agent QA drive a Runtime Host they own: the
    factory's per-tick host, ``make dev``, simlab, or the dedicated canary. A
    hosted instance a person uses must never absorb that traffic by accident.
    """

    if not api_url:
        return
    host = urllib.parse.urlparse(api_url).hostname or ""
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    if os.environ.get(HOSTED_TARGET_ENV, "").rstrip("/") == api_url.rstrip("/"):
        return
    raise RuntimeError(
        f"refusing live QA against {api_url}: run it against a local Runtime Host "
        f"(make dev, simlab) or the canary; set {HOSTED_TARGET_ENV}={api_url} to target it deliberately"
    )
