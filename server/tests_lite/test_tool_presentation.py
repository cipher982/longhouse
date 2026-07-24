from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import zerg.services.tool_presentation as tool_presentation_module
from zerg.services.session_views import build_event_response
from zerg.services.shell_command_summary import MAX_SOURCE_CHARS
from zerg.services.shell_command_summary import summarize_shell_source
from zerg.services.tool_presentation import extract_codex_wrapper_calls
from zerg.services.tool_presentation import project_tool_presentation


def test_runtime_image_copies_tool_presentation_rules():
    root = Path(__file__).resolve().parents[2]
    dockerfile = root / "docker" / "runtime.dockerfile"
    dockerignore = root / ".dockerignore"

    assert "COPY config/tool-tiers.json /config/tool-tiers.json" in dockerfile.read_text(encoding="utf-8")
    assert "!config/tool-tiers.json" in dockerignore.read_text(encoding="utf-8")


def test_shell_command_summary_conformance_corpus():
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads((root / "config" / "shell-command-summary-fixtures.json").read_text(encoding="utf-8"))

    for case in fixture["cases"]:
        summary = summarize_shell_source(case["source"])
        assert summary is not None, case["name"]
        assert summary["confidence"] == case["expected_confidence"], case["name"]
        assert [item["label"] for item in summary["operations"]] == case["expected_labels"], case["name"]
        assert [item["count"] for item in summary["operations"]] == case["expected_counts"], case["name"]
        if "expected_dynamic" in case:
            assert summary["dynamic"] is case["expected_dynamic"], case["name"]
        if "expected_parse_error" in case:
            assert summary["parse_error"] == case["expected_parse_error"], case["name"]
        serialized = json.dumps(summary, sort_keys=True)
        for forbidden in case.get("forbidden", []):
            assert forbidden not in serialized, f"{case['name']}: leaked {forbidden!r}"


def test_shell_command_summary_bounds_large_source_without_leaking_tail():
    secret_tail = "private-tail-token"
    summary = summarize_shell_source("gh run view 1 " + ("x" * MAX_SOURCE_CHARS) + secret_tail)

    assert summary is not None
    assert summary["truncated"] is True
    assert summary["parse_error"] == "source_limit"
    assert secret_tail not in json.dumps(summary)


def test_direct_shell_projection_attaches_summary_without_mutating_input():
    payload = {"cmd": "TOKEN=private gh run view 123"}

    presentation = project_tool_presentation("exec_command", payload, provider="codex")

    assert presentation is not None
    assert presentation["tool_input_json"] == payload
    assert presentation["shell_summary"]["confidence"] == "syntactic"
    assert [item["label"] for item in presentation["shell_summary"]["operations"]] == ["gh run view"]
    assert "private" not in json.dumps(presentation["shell_summary"])


def test_every_configured_shell_tool_receives_the_same_summary_contract():
    root = Path(__file__).resolve().parents[2]
    rules = json.loads((root / "config" / "tool-tiers.json").read_text(encoding="utf-8"))

    for tool_name in rules["shell_classifier"]["shell_tools"]:
        source_key = "command" if tool_name == "Bash" else "cmd"
        presentation = project_tool_presentation(tool_name, {source_key: "gh run view 123"})

        assert presentation is not None, tool_name
        assert presentation["shell_summary"]["operations"][0]["label"] == "gh run view", tool_name


def test_extracts_single_codex_exec_command_without_executing_wrapper():
    source = 'const r = await tools.exec_command({cmd:"rg -n \'needle\' server"}); text(r.output);'

    calls, complete = extract_codex_wrapper_calls(source)

    assert complete is True
    assert calls == [
        {
            "tool_name": "exec_command",
            "tool_input_json": {"cmd": "rg -n 'needle' server"},
            "input_complete": True,
            "source_span": [16, 65],
            "result_forwarded": True,
        }
    ]


def test_single_child_wrapper_recedes_only_with_forwarded_result():
    source = 'const r=await tools.write_stdin({session_id:87859,chars:"",yield_time_ms:30000}); text(r);'

    presentation = project_tool_presentation("exec", source, provider="codex")

    assert presentation is not None
    assert presentation["disposition"] == "parsed"
    assert presentation["tool_name"] == "write_stdin"
    assert presentation["wrapper_recedes"] is True
    assert presentation["execution_method"] == "exec"
    assert presentation["source_tool_name"] == "exec"
    assert presentation["children"][0]["label"] == "Wait"
    assert presentation["children"][0]["aggregate"] == "wait"
    assert presentation["children"][0]["tool_input_json"]["session_id"] == 87859


def test_direct_forwarded_patch_resolves_local_literal_and_recedes():
    source = '''const patch = "*** Begin Patch\\n*** Update File: app.py\\n*** End Patch";
text(await tools.apply_patch(patch));'''

    presentation = project_tool_presentation("exec", source, provider="codex")

    assert presentation is not None
    assert presentation["tool_name"] == "apply_patch"
    assert presentation["label"] == "Edited"
    assert presentation["wrapper_recedes"] is True
    assert presentation["tool_input_json"] == {
        "patch": "*** Begin Patch\n*** Update File: app.py\n*** End Patch"
    }


def test_patch_literal_resolution_ignores_comment_and_string_decoys():
    source = '''const patch = "*** Begin Patch\\n*** Update File: real.py\\n*** End Patch";
const example = "const patch = \\\"wrong\\\"";
// const patch = "also wrong";
text(await tools.apply_patch(patch));'''

    presentation = project_tool_presentation("exec", source, provider="codex")

    assert presentation is not None
    assert presentation["wrapper_recedes"] is True
    assert "real.py" in presentation["tool_input_json"]["patch"]


def test_patch_literal_resolution_fails_open_after_dynamic_reassignment():
    source = '''let patch = "*** Begin Patch\\n*** Update File: stale.py\\n*** End Patch";
patch = patch + dynamicPart();
text(await tools.apply_patch(patch));'''

    presentation = project_tool_presentation("exec", source, provider="codex")

    assert presentation is not None
    assert presentation["wrapper_recedes"] is False
    assert presentation["tool_name"] == "exec"


def test_single_child_without_forwarded_result_keeps_wrapper_prominent():
    source = 'await tools.exec_command({cmd:"dangerous"}); text("done");'

    presentation = project_tool_presentation("exec", source, provider="codex")

    assert presentation is not None
    assert presentation["tool_name"] == "exec"
    assert presentation["wrapper_recedes"] is False


def test_multi_child_wrapper_stays_prominent_and_preserves_children():
    source = """const [a,b] = await Promise.all([
      tools.exec_command({cmd:'pwd'}),
      tools.mcp__longhouse__search_sessions({query:'patent',limit:20})
    ]); text(a);"""

    presentation = project_tool_presentation("exec", source, provider="codex")

    assert presentation is not None
    assert presentation["disposition"] == "parsed"
    assert presentation["label"] == "Called 2 tools"
    assert presentation["wrapper_recedes"] is False
    assert [child["tool_name"] for child in presentation["children"]] == [
        "exec_command",
        "mcp__longhouse__search_sessions",
    ]
    assert presentation["children"][1]["disposition"] == "generic"
    assert "shell_summary" not in presentation


def test_single_forwarded_shell_child_recedes_with_summary():
    source = 'const r=await tools.exec_command({cmd:"gh run view 123"}); text(r.output);'

    presentation = project_tool_presentation("exec", source, provider="codex")

    assert presentation is not None
    assert presentation["wrapper_recedes"] is True
    assert presentation["tool_name"] == "exec_command"
    assert presentation["source_tool_name"] == "exec"
    assert [item["label"] for item in presentation["shell_summary"]["operations"]] == ["gh run view"]
    assert "shell_summary" not in presentation["children"][0]


def test_unparsed_exec_fails_open_as_unknown():
    presentation = project_tool_presentation("exec", "runSomethingDynamic()", provider="codex")

    assert presentation is not None
    assert presentation["disposition"] == "unknown"
    assert presentation["wrapper_recedes"] is False
    assert presentation["tool_name"] == "exec"


def test_strings_and_comments_do_not_create_false_children():
    source = """const example = 'tools.exec_command({cmd:\"bad\"})';
    // tools.write_stdin({session_id: 1})
    const r = await tools.exec_command({cmd:'pwd'});"""

    calls, complete = extract_codex_wrapper_calls(source)

    assert complete is True
    assert [call["tool_name"] for call in calls] == ["exec_command"]


def test_codex_wrapper_rule_cannot_fire_for_another_provider():
    source = 'const r=await tools.exec_command({cmd:"pwd"}); text(r.output);'

    presentation = project_tool_presentation("exec", source, provider="claude")

    assert presentation is not None
    assert presentation["disposition"] == "exact"
    assert presentation["wrapper_recedes"] is False


def test_wait_alias_is_scoped_to_codex():
    payload = {"session_id": 42, "chars": ""}

    codex = project_tool_presentation("write_stdin", payload, provider="codex")
    claude = project_tool_presentation("write_stdin", payload, provider="claude")

    assert codex is not None and codex["label"] == "Wait" and codex["aggregate"] == "wait"
    assert claude is not None and claude["label"] == "stdin" and claude["aggregate"] is None


def test_event_response_projects_presentation_without_mutating_raw_tool_input():
    raw_input = 'const r=await tools.exec_command({cmd:"pwd"}); text(r.output);'
    event = SimpleNamespace(
        id=1,
        role="assistant",
        content_text=None,
        tool_name="exec",
        tool_input_json=raw_input,
        tool_output_text=None,
        tool_call_id="call-1",
        timestamp=datetime(2026, 7, 23, tzinfo=timezone.utc),
        branch_id=None,
        event_origin="durable",
        provisional_state=None,
        provisional_cursor=None,
        provisional_complete=False,
        reconciled_event_id=None,
    )
    response = build_event_response(
        SimpleNamespace(provider="codex"),
        event,
        boundary=None,
        head_branch_id=None,
        input_origin_map={},
        provider="codex",
    )

    assert response.tool_input_json == raw_input
    assert response.tool_presentation is not None
    assert response.tool_presentation.disposition == "parsed"
    assert response.tool_presentation.children[0].tool_name == "exec_command"
    assert response.tool_presentation.wrapper_recedes is True
    assert response.tool_presentation.shell_summary is not None
    assert response.tool_presentation.shell_summary.operations[0].label == "pwd"


def test_default_rules_path_prefers_packaged_copy(tmp_path, monkeypatch):
    fake_module = tmp_path / "site-packages" / "zerg" / "services" / "tool_presentation.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# fake module path\n", encoding="utf-8")
    packaged_rules = fake_module.parents[1] / "_config" / "tool-tiers.json"
    packaged_rules.parent.mkdir(parents=True)
    packaged_rules.write_text('{"tools": {}}', encoding="utf-8")

    monkeypatch.setattr(tool_presentation_module, "__file__", str(fake_module))

    assert tool_presentation_module._get_default_rules_path() == packaged_rules
