"""Canonical tool vocabulary: intent precedence, served intent source, status line."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.services.session_state_contract import SessionDelegationFacts
from zerg.services.session_state_contract import _delegation_label
from zerg.services.tool_presentation import project_tool_presentation
from zerg.services.tool_presentation import resolve_tool_intent
from zerg.services.tool_presentation import tool_display_label
from zerg.services.tool_presentation import tool_status_phrase

ROOT = Path(__file__).resolve().parents[2]
RULES = json.loads((ROOT / "config" / "tool-tiers.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _test_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("TESTING", "1")


def _served(tool_name, tool_input, *, provider, item_intent=None, facts=None):
    """The served (tool_intent, tool_intent_source) for one durable workspace event."""
    from zerg.services.storage_v2_workspace import _event_projection

    event = {
        "event_id": "e1",
        "cursor": "c1",
        "role": "assistant",
        "timestamp": "2026-11-26T00:00:00Z",
        "tool_name": tool_name,
        "tool_input_json": tool_input,
        "tool_call_id": "tc-1",
        "raw_locator": {"source_position": 3, "event_subordinal": 0},
        "item": {"intent": item_intent} if item_intent else None,
    }
    projected = _event_projection(
        event,
        session_id=uuid4(),
        closed=False,
        provider=provider,
        completed_tool_call_ids=set(),
        cursor_run_ended=False,
        facts_by_pos={3: facts} if facts else None,
    )
    return projected["event"]["tool_intent"], projected["event"]["tool_intent_source"]


# --- vocabulary -------------------------------------------------------------


def test_every_label_is_canonical_and_every_entry_has_a_status():
    labels = RULES["labels"]
    assert set(labels) == {
        "Shell",
        "Read",
        "Edit",
        "Write",
        "Notebook",
        "Search",
        "List",
        "Fetch",
        "Web search",
        "Agent",
        "Plan",
        "Ask",
        "Wait",
        "Result",
        "Tool",
    }
    lowered = [name.lower() for name in RULES["tools"]]
    assert len(lowered) == len(set(lowered)), "names differing only in case duplicate a case-insensitive lookup"
    for name, meta in RULES["tools"].items():
        assert meta["label"] in labels, name
        assert meta["status"].strip(), name


@pytest.mark.parametrize(
    ("provider", "tool_name", "label"),
    [
        ("claude", "Bash", "Shell"),
        ("codex", "exec_command", "Shell"),
        ("cursor", "Shell", "Shell"),
        ("opencode", "bash", "Shell"),
        ("antigravity", "run_command", "Shell"),
        ("claude", "Read", "Read"),
        ("opencode", "read", "Read"),
        ("antigravity", "read_file", "Read"),
        ("antigravity", "view_file", "Read"),
        ("claude", "Edit", "Edit"),
        ("codex", "apply_patch", "Edit"),
        ("cursor", "StrReplace", "Edit"),
        ("antigravity", "edit_file", "Edit"),
        ("antigravity", "write_file", "Write"),
        ("omp", "grep", "Search"),
        ("omp", "find", "Search"),
        ("omp", "glob", "List"),
        ("opencode", "list", "List"),
        ("opencode", "webfetch", "Fetch"),
        ("omp", "web_fetch", "Fetch"),
        ("omp", "web_search", "Web search"),
        ("claude", "WebSearch", "Web search"),
        ("codex", "spawn_agent", "Agent"),
        ("antigravity", "INVOKE_SUBAGENT", "Agent"),
        ("codex", "update_plan", "Plan"),
        ("opencode", "todowrite", "Plan"),
        ("antigravity", "manage_task", "Plan"),
        ("omp", "ask", "Ask"),
        ("cursor", "AskQuestion", "Ask"),
        ("codex", "wait", "Wait"),
        ("codex", "wait_agent", "Wait"),
        ("omp", "eval", "Shell"),
    ],
)
def test_every_provider_spelling_resolves_to_one_canonical_label(provider, tool_name, label):
    presentation = project_tool_presentation(tool_name, {}, provider=provider)
    assert presentation is not None
    assert presentation["disposition"] == "exact", tool_name
    assert presentation["label"] == label


def test_antigravity_run_command_gets_a_shell_summary_from_command_line():
    presentation = project_tool_presentation("run_command", {"CommandLine": "gh run view 123"}, provider="antigravity")
    assert presentation is not None
    assert presentation["shell_summary"]["operations"][0]["label"] == "gh run view"


# --- intent precedence ------------------------------------------------------


def test_omp_authored_i_beats_the_derived_path_and_command():
    # Engine item intent carries OMP's block `intent` / `arguments.i`.
    assert _served("read", {"path": "/tmp/a.txt", "i": "Read the notes"}, provider="omp", item_intent="Read the notes") == (
        "Read the notes",
        "authored",
    )
    # Block-level intent only (no `i` in arguments): the engine intent still wins.
    assert _served("bash", {"command": "make test"}, provider="omp", item_intent="Run the test suite") == (
        "Run the test suite",
        "authored",
    )
    # Older stored rows without the engine intent: `i` is read server-side.
    assert _served("grep", {"pattern": "^#", "i": "Locating product docs"}, provider="omp") == (
        "Locating product docs",
        "authored",
    )
    assert _served("eval", {"code": "x", "title": "Reading machine state"}, provider="omp") == (
        "Reading machine state",
        "authored",
    )


def test_claude_description_is_authored_only_on_purposeful_tools():
    assert _served("Bash", {"command": "make test", "description": "Run the test suite"}, provider="claude") == (
        "Run the test suite",
        "authored",
    )
    assert _served("Bash", {"command": "python3 test_inventory.py"}, provider="claude") == (
        "python3 test_inventory.py",
        "derived",
    )
    assert _served("Read", {"file_path": "/tmp/items/parser.rs"}, provider="claude") == ("items/parser.rs", "derived")
    # An MCP tool's description is its payload, not why it was called, even
    # when Claude's tool.intent fact copied it.
    facts = {"tool.intent": {"tool_call_id": "tc-1", "intent": "Fix the login bug"}}
    assert _served(
        "mcp__life-hub-agents__create_task",
        {"title": "Bug", "description": "Fix the login bug"},
        provider="claude",
        facts=facts,
    ) == (None, None)
    assert _served("Artifact", {"intent": "other", "description": "A page"}, provider="claude") == (None, None)


def test_antigravity_tool_summary_beats_the_command_line():
    args = {"CommandLine": "sleep 6", "WaitMsBeforeAsync": 10000, "toolSummary": "Sleep 6 seconds"}
    assert _served("run_command", args, provider="antigravity", item_intent="Sleep 6 seconds") == ("Sleep 6 seconds", "authored")
    assert _served("run_command", args, provider="antigravity") == ("Sleep 6 seconds", "authored")
    assert _served("read_file", {"Path": "/work/docs/b.md"}, provider="antigravity") == ("docs/b.md", "derived")


def test_codex_has_no_authored_intent_so_everything_is_derived():
    assert _served("exec_command", {"cmd": "make build\nmake test"}, provider="codex") == ("make build", "derived")
    assert _served("web_search", {"query": "metal texture coordinates"}, provider="codex") == (
        "metal texture coordinates",
        "derived",
    )
    assert _served("update_plan", {"plan": []}, provider="codex") == (None, None)


def test_cursor_and_opencode_authored_descriptions():
    assert resolve_tool_intent("Shell", {"command": "ls", "description": "List the files"}) == ("List the files", "authored")
    assert resolve_tool_intent("task", {"description": "Review diff", "prompt": "Review the diff"}) == ("Review diff", "authored")
    assert resolve_tool_intent("read", {"filePath": "/repo/app/auth.py"}) == ("app/auth.py", "derived")


# --- served status line -----------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "phrase", "label"),
    [
        ("exec_command", "Running a command", "Shell"),
        ("Bash", "Running a command", "Shell"),
        ("shell", "Running a command", "Shell"),
        ("read", "Reading files", "Read"),
        ("edit", "Editing files", "Edit"),
        ("Write", "Writing a file", "Write"),
        ("grep", "Searching", "Search"),
        ("Glob", "Listing files", "List"),
        ("WebFetch", "Fetching a page", "Fetch"),
        ("web_search", "Searching the web", "Web search"),
        ("Agent", "Running a subagent", "Agent"),
        ("update_plan", "Updating the plan", "Plan"),
        ("AskUserQuestion", "Asking a question", "Ask"),
        ("wait", "Waiting", "Wait"),
        ("mcp__trove__search_personal_memory", "Using search personal memory", "search personal memory"),
        # Words after the name that are not JSON make it a command line.
        ("echo {a,b}", "Running a command", "Shell"),
        # Antigravity ACP puts the command line, or `name {json}`, in the tool slot.
        ("sleep 6", "Running a command", "Shell"),
        ("find . -name '*.py'", "Running a command", "Shell"),
        ('view_file {"AbsolutePath": "/x"}', "Reading files", "Read"),
        ('mystery_tool {"a": 1}', "Using mystery_tool", "mystery_tool"),
        ("hatch_codex", "Using hatch_codex", "hatch_codex"),
        ("x" * 50, "Using " + "x" * 32, "x" * 32),
    ],
)
def test_status_phrase_never_shows_a_raw_known_name_or_a_command_line(tool, phrase, label):
    assert tool_status_phrase(tool) == phrase
    assert tool_display_label(tool) == label


def test_status_phrase_is_none_without_a_tool():
    assert tool_status_phrase(None) is None
    assert tool_status_phrase("  ") is None


@pytest.mark.parametrize(
    ("kinds", "count", "label"),
    [
        ({"subagent": 2, "shell": 1}, 3, "Waiting on 2 subagents · 1 command"),
        ({"subagent": 1}, 1, "Waiting on 1 subagent"),
        ({"shell": 3}, 3, "Waiting on 3 commands"),
        ({"monitor": 1, "subagent": 0}, 1, "Waiting on 1 monitor"),
        ({}, 2, "Waiting on 2 background tasks"),
        ({"mystery": 1}, 1, "Waiting on 1 task"),
    ],
)
def test_delegation_label_wording(kinds, count, label):
    assert _delegation_label(SessionDelegationFacts(state="pending", count=count, kinds=kinds)) == label
