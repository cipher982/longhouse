"""`live_session_toolkit` is the shared live-session library, not a producer's privates.

`provider_native_resume.py` was a 4,485-line de-facto toolkit wearing a resume
producer's name: seventeen sibling producers imported its privates, which is
what the missing library layer looked like. The shared half now lives here with
public names. These tests keep the boundary from eroding back.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
import re

import pytest

import zerg.qa
from zerg.qa import live_session_toolkit
from zerg.qa import provider_native_resume

QA_DIR = pathlib.Path(zerg.qa.__file__).parent
TOOLKIT = QA_DIR / "live_session_toolkit.py"

# The resume producer keeps these: they are the producer, not the toolkit.
RESUME_PRODUCER_EXPORTS = {"main_for", "registration_for", "SPECS"}


def _public_names(path: pathlib.Path) -> set[str]:
    return {
        node.name
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_")
    }


def test_no_sibling_reaches_into_the_resume_producers_privates() -> None:
    """The reach-through this extraction existed to remove must not come back."""
    offenders = []
    for path in QA_DIR.glob("*.py"):
        if path.name == "provider_native_resume.py":
            continue
        for match in re.finditer(r"from zerg\.qa\.provider_native_resume import (\w+)", path.read_text(encoding="utf-8")):
            if match.group(1).startswith("_"):
                offenders.append(f"{path.name}: {match.group(1)}")
    assert offenders == []


def test_siblings_only_import_the_resume_producers_own_surface() -> None:
    imported: set[str] = set()
    for path in QA_DIR.glob("*.py"):
        if path.name == "provider_native_resume.py":
            continue
        imported.update(re.findall(r"from zerg\.qa\.provider_native_resume import (\w+)", path.read_text(encoding="utf-8")))
    assert imported <= RESUME_PRODUCER_EXPORTS


def test_toolkit_exposes_the_shared_surface_publicly() -> None:
    """Everything a sibling needs is public; nothing needs an underscore."""
    public = _public_names(TOOLKIT)
    for expected in (
        "isolated_provider_home",
        "launch_command",
        "qualification_secrets",
        "redact_state_for_evidence",
        "secret_scan",
        "start_transcript_shipper",
        "stop_session",
        "wait_state",
        "wait_session_tail",
        "wait_assistant_response_after_marker",
        "write_json",
        "PtyProcess",
        "TranscriptShipper",
    ):
        assert expected in public, expected


def test_toolkit_does_not_import_the_resume_producer() -> None:
    """The dependency runs one way: producers depend on the library."""
    imported = {
        node.module for node in ast.walk(ast.parse(TOOLKIT.read_text(encoding="utf-8"))) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.startswith("zerg.qa.") and module.endswith("_resume") for module in imported)


def test_resume_producer_reaches_the_toolkit_through_the_module() -> None:
    """One patch point.

    While the producer held `from ... import name` bindings, a test patching
    `live_session_toolkit.name` silently did nothing for producer-side callers
    and a test patching the producer did nothing for toolkit-side callers. Two
    dozen tests were quietly exercising the real sleeps and sockets as a result.
    """
    source = (QA_DIR / "provider_native_resume.py").read_text(encoding="utf-8")
    assert "from zerg.qa import live_session_toolkit" in source
    assert re.search(r"from zerg\.qa\.live_session_toolkit import", source) is None


@pytest.mark.parametrize("name", sorted(RESUME_PRODUCER_EXPORTS))
def test_resume_producer_still_exports_its_own_surface(name: str) -> None:
    assert hasattr(provider_native_resume, name)


def test_toolkit_is_smaller_than_the_producer_it_was_carved_out_of() -> None:
    """A guard against the producer silently reabsorbing shared mechanics."""
    producer_lines = len((QA_DIR / "provider_native_resume.py").read_text(encoding="utf-8").splitlines())
    assert producer_lines < 2500, "shared live-session mechanics belong in live_session_toolkit"


def test_toolkit_write_json_is_atomic() -> None:
    """Evidence writers must not leave a torn file behind a crash."""
    source = ast.unparse(
        next(
            node
            for node in ast.parse(TOOLKIT.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef) and node.name == "write_json"
        )
    )
    assert ".tmp" in source
    assert "replace(path)" in source


def test_toolkit_write_json_replaces_atomically(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "evidence.json"
    target.write_text('{"stale": true}')

    live_session_toolkit.write_json(target, {"fresh": True})

    assert target.read_text(encoding="utf-8") == '{\n  "fresh": true\n}\n'
    assert [p.name for p in tmp_path.iterdir()] == ["evidence.json"]


def test_qualification_session_retirement_paginates_served_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            {"hidden": True},
            {"user_state": "archived"},
            {"sessions": [{"id": "other-1"}, {"id": "other-2"}], "total": 3},
            {"sessions": [{"id": "other-3"}], "total": 3},
        ]
    )
    requests: list[tuple[str, str]] = []

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def opener(request: object, *, timeout: float) -> Response:
        del timeout
        requests.append((request.get_method(), request.full_url))  # type: ignore[attr-defined]
        return Response(next(responses))

    monkeypatch.setattr(live_session_toolkit.urllib.request, "urlopen", opener)

    receipt = live_session_toolkit.retire_qualification_session(
        "http://runtime.example",
        "agents-token",
        "session-1",
        provider="pi",
        project="provider-console-pi",
    )

    assert receipt["status"] == "pass"
    assert receipt["hidden"] is True
    assert receipt["archived"] is True
    assert receipt["present_in_served_inventory"] is False
    assert receipt["served_inventory_total"] == 3
    assert requests[0][0] == "PATCH"
    assert requests[1][0] == "POST"
    assert "offset=0" in requests[2][1]
    assert "offset=2" in requests[3][1]


def test_qualification_session_retirement_retries_transient_action_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []
    responses = iter(
        [
            RuntimeError("PATCH /api/agents/sessions/session-1/timeline-visibility returned HTTP 503: busy"),
            {"hidden": True},
        ]
    )

    def request(method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((method, path, payload))
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(live_session_toolkit.time, "sleep", lambda _seconds: None)

    hidden, retries = live_session_toolkit._retirement_action_request(
        request,
        "PATCH",
        "/api/agents/sessions/session-1/timeline-visibility",
        {"hidden": True},
        retry_count=0,
        transient_errors=[],
    )

    assert hidden == {"hidden": True}
    assert retries == 1
    assert len(calls) == 2


def test_qualification_session_retirement_retries_transient_inventory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transient = live_session_toolkit.urllib.error.HTTPError(
        "http://runtime.example/api/agents/sessions",
        503,
        "busy",
        {},
        io.BytesIO(b'{"detail": "busy"}'),
    )
    responses = iter(
        [
            {"hidden": True},
            {"user_state": "archived"},
            transient,
            {"sessions": [], "total": 0},
        ]
    )
    requests: list[tuple[str, str]] = []

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def opener(request: object, *, timeout: float) -> Response:
        del timeout
        requests.append((request.get_method(), request.full_url))  # type: ignore[attr-defined]
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return Response(value)

    monkeypatch.setattr(live_session_toolkit.urllib.request, "urlopen", opener)
    monkeypatch.setattr(live_session_toolkit.time, "sleep", lambda _seconds: None)

    receipt = live_session_toolkit.retire_qualification_session(
        "http://runtime.example",
        "agents-token",
        "session-1",
        provider="omp",
    )

    assert receipt["status"] == "pass"
    assert receipt["served_inventory_retry_count"] == 1
    assert receipt["served_inventory_transient_errors"] == ["HTTP 503"]
    assert requests[2][0] == "GET"
    assert requests[3][0] == "GET"


def test_qualification_secrets_include_provider_specific_live_keys() -> None:
    secrets = live_session_toolkit.qualification_secrets(
        {
            "PI_OPENROUTER_API_KEY": "pi-secret",
            "OMP_OPENROUTER_API_KEY": "omp-secret",
        },
        "agents-token",
    )

    assert {"pi-secret", "omp-secret", "agents-token"} <= set(secrets)
