"""`live_session_toolkit` is the shared live-session library, not a producer's privates.

`provider_native_resume.py` was a 4,485-line de-facto toolkit wearing a resume
producer's name: seventeen sibling producers imported its privates, which is
what the missing library layer looked like. The shared half now lives here with
public names. These tests keep the boundary from eroding back.
"""

from __future__ import annotations

import ast
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
        node.module
        for node in ast.walk(ast.parse(TOOLKIT.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
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
