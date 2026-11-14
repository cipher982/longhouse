"""The shape catalogs hold every shape the parsers' arms can produce.

A parser that dispatches on each content block handles every combination of
the blocks it has an arm for. The catalogs state those arms once, as
families, and the census tool derives the row of any combination. These tests
tie the families to the parsers from both sides: the arms are read out of the
parser sources, so a new arm fails here until the catalog names it, and every
shape those arms can produce is built and checked against the catalog.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "qa" / "transcript_census.py"
ENGINE = REPO_ROOT / "engine" / "src"
PARSER = "pipeline/parser.rs"

# What stands for a message's content when it is not a list of typed blocks.
STRING, EMPTY = "string", "?"


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("transcript_census_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog(provider: str) -> dict:
    return json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / f"{provider}.json").read_text())


# ---------------------------------------------------------------------------
# The parsers' arms, read from their sources
# ---------------------------------------------------------------------------

_ARM = re.compile(r'^\s*((?:Some\()?"[^"\n]*"\)?(?:\s*\|\s*(?:Some\()?"[^"\n]*"\)?)*)\s*=>', re.MULTILINE)


def _rust_function(path: str, name: str) -> str:
    text = (ENGINE / path).read_text()
    start = re.search(rf"^(?:pub(?:\([a-z]+\))? )?fn {name}\b", text, re.MULTILINE)
    assert start is not None, f"{path} no longer has fn {name}; point this test at the parser's new arms"
    end = re.search(r"^}$", text[start.start() :], re.MULTILINE)
    assert end is not None
    return text[start.start() : start.start() + end.end()]


def _arms(path: str, name: str) -> set[str]:
    """The string literals a function's `match` arms name."""
    return {literal for arm in _ARM.findall(_rust_function(path, name)) for literal in re.findall(r'"([^"]*)"', arm)}


def _literals(path: str, name: str) -> set[str]:
    return set(re.findall(r'"([^"]*)"', _rust_function(path, name)))


def _record_rows(catalog: dict) -> set[str]:
    """Rows for whole-line record types, without the row that stands for an unknown one."""
    return {
        shape
        for shape, row in catalog["shapes"].items()
        if "/" not in shape and not str(row["classification"]).startswith("provider_drift:")
    }


def _subsets(names: list[str]) -> list[tuple[str, ...]]:
    """Every content a message can carry from these: each non-empty set of blocks, a string, nothing."""
    blocks = [name for name in names if name not in (STRING, EMPTY)]
    combinations = [combo for size in range(1, len(blocks) + 1) for combo in itertools.combinations(blocks, size)]
    return combinations + [(name,) for name in names if name in (STRING, EMPTY)]


def _content(combo: tuple[str, ...], text: str = "x"):
    if combo == (STRING,):
        return text
    if combo == (EMPTY,):
        return []
    return [{"type": name, "text": text} for name in combo]


def _assert_all_classified(provider: str, records: list[dict]) -> int:
    """Every record's shape is classified by its catalog; returns how many distinct shapes that was."""
    tool = _tool()
    catalog = _catalog(provider)
    shapes = {tool.SHAPERS[provider](record)[0] for record in records}
    observed = {shape: {"count": 1, "keys": [], "key_counts": {}} for shape in shapes}
    assert tool.unclassified(catalog, observed) == [], provider
    return len(shapes)


# Claude's `extract_assistant_events` makes an event from each block it has an arm for and passes over
# every other block in its catch-all arm. These are the blocks the catalog names that the parser passes
# over: the catalog says what each is, and the parser makes no event from it.
CLAUDE_BLOCKS_WITHOUT_AN_ARM = {"thinking", "fallback"}


def test_claude_assistant_family_names_every_block_the_parser_has_an_arm_for():
    tool = _tool()
    catalog = _catalog("claude")
    (family,) = [family for family in catalog["families"] if family["segments"][0] == "assistant"]
    names = family["segments"][1]

    # An arm makes a transcript event from its block, so the catalog names the block and calls it that.
    arms = _arms(PARSER, "extract_assistant_events")
    assert arms, "extract_assistant_events no longer dispatches on string arms; point this test at its new shape"
    assert arms <= set(names), f"the Claude assistant family does not name {sorted(arms - set(names))}"
    assert {names[arm]["classification"] for arm in arms} == {"transcript"}

    # Every other name is a block the catch-all passes over, and this test lists them. A name added to the
    # catalog joins that list or gets an arm. A listed block that gains an arm leaves the list, and by the
    # assertion above its catalog entry must then read `transcript`: an arm for a block the catalog calls a
    # signal fails until the catalog says the parser makes a transcript event from it.
    assert re.search(r"^\s*_ => \{$", _rust_function(PARSER, "extract_assistant_events"), re.MULTILINE)
    assert set(names) - arms == CLAUDE_BLOCKS_WITHOUT_AN_ARM

    # Every combination of those blocks on one line is a shape the catalog classifies: fifteen from four.
    records = [{"type": "assistant", "message": {"content": _content(combo)}} for combo in _subsets(sorted(names))]
    assert _assert_all_classified("claude", records) == 2 ** len(names) - 1

    # A block the catalog does not name makes its line a finding, alone or beside blocks it does name.
    for content in ([{"type": "server_tool_use"}], [{"type": "text"}, {"type": "server_tool_use"}]):
        shape = tool.SHAPERS["claude"]({"type": "assistant", "message": {"content": content}})[0]
        assert tool.unclassified(catalog, {shape: {"count": 1, "keys": []}}) == [shape]


# Pi and OMP share `extract_pi_message_events`: role -> the content it has a path for.
PI_MESSAGE_ARMS = {
    "user": [EMPTY, STRING, "text", "image"],
    "assistant": [STRING, "text", "thinking", "image", "toolCall"],
    "toolResult": [EMPTY, STRING, "text", "image"],
    "system": [EMPTY, STRING, "text"],
    "custom": [EMPTY, STRING, "text"],
    "custom_message": [EMPTY, STRING, "text"],
    "bashExecution": [EMPTY, STRING, "text"],
    "branchSummary": [EMPTY, STRING, "text"],
    "compactionSummary": [EMPTY, STRING, "text"],
}


@pytest.mark.parametrize(("provider", "line_types"), [("pi", "is_pi_line"), ("omp", "is_omp_line")])
def test_pi_and_omp_catalogs_hold_every_shape_the_parser_has_a_path_for(provider: str, line_types: str):
    catalog = _catalog(provider)

    # A line type the parser recognizes is a row; `message` is the families below.
    assert _literals(PARSER, line_types) - {"message"} == _record_rows(catalog)
    assert _arms(PARSER, "extract_pi_events") <= _literals(PARSER, line_types)

    # The message arms are exactly the roles and blocks this test enumerates.
    blocks = {name for names in PI_MESSAGE_ARMS.values() for name in names} - {STRING, EMPTY}
    assert _arms(PARSER, "extract_pi_message_events") == set(PI_MESSAGE_ARMS) | blocks

    # A user message with no content yields no event, and neither does a system message, whatever it
    # carries: it is the prompt and the tools the model was given. Pi leaves both at that; for OMP a line
    # that yields nothing is recorded as an unknown record (`extract_omp_events`), so there they stay findings.
    assert "push_omp_record_event(obj, session_id, line_offset, raw_line, true, events)" in _rust_function(PARSER, "extract_omp_events")
    assert re.search(r'^\s*"system" => \{\}$', _rust_function(PARSER, "extract_pi_message_events"), re.MULTILINE)
    without_an_event = {"user": [EMPTY], "system": PI_MESSAGE_ARMS["system"]}
    records = [{"type": line_type} for line_type in _record_rows(catalog)]
    for role, names in PI_MESSAGE_ARMS.items():
        names = [name for name in names if provider == "pi" or name not in without_an_event.get(role, [])]
        records += [{"type": "message", "message": {"role": role, "content": _content(combo)}} for combo in _subsets(names)]
    records.append({"type": "message", "message": {"role": "bashExecution", "command": "pwd"}})
    # An assistant message with no content is how a failed request is written.
    records.append({"type": "message", "message": {"role": "assistant", "content": [], "stopReason": "error"}})
    # user 4 (and its empty message for Pi), assistant 16, toolResult 5, the five other roles 3 each,
    # the empty assistant message, and for Pi the three system messages.
    assert _assert_all_classified(provider, records) == len(_record_rows(catalog)) + 4 + 4 * (provider == "pi") + 16 + 5 + 15 + 1

    # What the parser has no arm for stays a finding, and for OMP so does a message that yields no event.
    tool = _tool()
    for record in (
        *(
            {"type": "message", "message": {"role": role, "content": _content(combo)}}
            for role, names in without_an_event.items()
            for combo in _subsets(names)
            if provider == "omp"
        ),
        {"type": "message", "message": {"role": "developer", "content": "instructions"}},
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "text"}, {"type": "video"}]}},
        {"type": "message", "message": {"role": "user", "content": [{"type": "thinking"}]}},
        {"type": "brand_new_line"},
    ):
        shape = tool.SHAPERS[provider](record)[0]
        assert tool.unclassified(catalog, {shape: {"count": 1, "keys": []}}) == [shape]


def test_opencode_catalog_holds_every_part_type_and_role_the_parser_has_a_path_for():
    part_types = _arms("opencode_db.rs", "extract_events_from_part")
    roles = _arms("opencode_db.rs", "event_role")
    assert part_types == {"text", "tool", "file", "patch", "reasoning", "step-start", "step-finish"}
    assert roles == {"user", "assistant", "system", "developer", "tool"}

    (family,) = _catalog("opencode")["families"]
    assert set(family["segments"][1]) == part_types
    assert set(family["segments"][2]) == roles

    records = [{"message": {"role": role}, "part": {"type": part_type}} for part_type in part_types for role in roles]
    assert _assert_all_classified("opencode", records) == 7 * 5

    tool = _tool()
    catalog = _catalog("opencode")
    for shape in ("part/video/assistant", "part/text/operator"):
        assert tool.unclassified(catalog, {shape: {"count": 1, "keys": []}}) == [shape]


def test_cursor_projection_catalog_holds_every_block_spelling_the_parser_has_a_path_for():
    arms = _arms(PARSER, "extract_cursor_events")
    roles = {"user", "assistant", "tool", "system"}
    blocks = arms - roles
    assert blocks == {
        "text",
        "reasoning",
        "redacted-reasoning",
        "tool-call",
        "tool_call",
        "tool-use",
        "tool_use",
        "tool-result",
        "tool_result",
    }
    family = _catalog("cursor")["families"][1]
    assert set(family["segments"][0]) == roles
    # The shaper also names a user message by what its text wraps.
    assert set(family["segments"][1]) == blocks | {STRING, "user_query", "provider_context"}

    texts = {"plain": "hello", "query": "<user_query>hello</user_query>", "context": "<user_info>\nOS: test\n</user_info>"}
    records = []
    for role in roles:
        for combo in _subsets([STRING, *sorted(blocks)]):
            for text in texts.values() if role == "user" else ["hello"]:
                records.append({"role": role, "message": {"content": _content(combo, text)}})
    # A line with a role and no message, or a message with no content, yields no event.
    cursor_events = _rust_function(PARSER, "extract_cursor_events")
    assert "let Some(message) = obj.message.as_ref() else {\n        return;" in cursor_events
    for role in roles:
        records += [{"role": role}, {"role": role, "message": {"content": []}}, {"role": role, "message": {}}]
    # 511 block combinations and a string, for each role; three ways to wrap a user's text; the empty line.
    assert _assert_all_classified("cursor", records) == 4 * 512 + 2 * 512 + 4

    tool = _tool()
    catalog = _catalog("cursor")
    for shape in ("assistant/image+text", "operator/text"):
        assert tool.unclassified(catalog, {shape: {"count": 1, "keys": []}}) == [shape]


def test_a_cursor_projection_line_is_named_by_the_precedence_the_engine_parses_it_by():
    # A line with a role goes to the Cursor arm before its `type` is looked at.
    dispatch = _rust_function(PARSER, "extract_events")
    by_role = dispatch.index("if obj.role.is_some() {\n        extract_cursor_events(")
    by_type = dispatch.index("let effective_role = match event_type {")
    assert by_role < by_type
    # A line that reaches the typed path is a message only if it has one, and is rendered for these types.
    assert "None => return," in dispatch[by_role:by_type]
    assert _arms(PARSER, "extract_events") >= {"message", "user", "assistant"}

    tool = _tool()
    catalog = _catalog("cursor")
    message = {"content": [{"type": "text", "text": "x"}]}
    named = {
        # role and type together: the role decides, and the Cursor arm renders it.
        "assistant/text": {"role": "assistant", "type": "turn_ended", "message": message},
        "tool/tool-result": {"role": "tool", "type": "assistant", "message": {"content": [{"type": "tool-result"}]}},
        # no role and no message: a lifecycle record.
        "event/turn_ended": {"type": "turn_ended", "status": "completed"},
        "event/note": {"type": "note", "role": None, "message": "not a message object"},
    }
    for shape, line in named.items():
        assert tool.SHAPERS["cursor"](line)[0] == shape
    assert tool.unclassified(catalog, _shapes_of(tool, named.values())) == ["event/note"]

    # No role but a message: the engine parses it by `type`, through the path Claude's lines take. That
    # path gives a Cursor line a new event id on every parse and the parse-time clock, so it is a finding.
    typed = {
        "type/assistant/text": {"type": "assistant", "message": message},
        "type/user/text": {"type": "user", "role": None, "message": {"content": [{"type": "text", "text": "<user_query>x</user_query>"}]}},
        "type/user/string": {"type": "message", "message": {"role": "user", "content": "x"}},
        "type/turn_ended/text": {"type": "turn_ended", "message": message},
    }
    for shape, line in typed.items():
        assert tool.SHAPERS["cursor"](line)[0] == shape
    assert tool.unclassified(catalog, _shapes_of(tool, typed.values())) == sorted(typed)


def _shapes_of(tool: ModuleType, lines) -> dict:
    return {tool.SHAPERS["cursor"](line)[0]: {"count": 1, "keys": []} for line in lines}


CURSOR_STORE_BLOCKS = {
    "text": {"type": "text", "text": "prose"},
    "reasoning": {"type": "reasoning", "text": "thought", "providerOptions": {}},
    "tool-call": {"type": "tool-call", "toolCallId": "call-1", "toolName": "Shell", "args": {"command": "true"}},
    "tool-result": {"type": "tool-result", "toolCallId": "call-1", "toolName": "Shell", "result": "ok"},
}


def _store_reader_arms() -> set[str]:
    source = (REPO_ROOT / "server" / "zerg" / "services" / "cursor_transcript.py").read_text()
    body = source[source.index("def _map_message(") : source.index("def decode_store_db(")]
    return set(re.findall(r'btype == "([^"]+)"', body))


def test_cursor_store_catalog_holds_every_block_combination_both_readers_have_a_path_for(tmp_path: Path):
    """Built as a real store, read by the server's reader and by `census check`."""
    from tests_lite.test_cursor_transcript import _write_store
    from zerg.services.cursor_transcript import decode_store_db

    engine_arms = _arms("storage_v2_shipper/cursor_store_source.rs", "render_cursor_records")
    assert engine_arms == set(CURSOR_STORE_BLOCKS)
    # The server's reader has one arm more; the engine renders that block as an empty row.
    assert _store_reader_arms() == engine_arms | {"redacted-reasoning"}
    family = _catalog("cursor")["families"][0]
    roles = set(family["segments"][1])
    assert roles == {"system", "user", "assistant", "tool"}
    assert set(family["segments"][2]) == engine_arms | {STRING}

    # A message with no content has no blocks, and both readers then render nothing.
    assert "_ => Vec::new()," in _rust_function("storage_v2_shipper/cursor_store_source.rs", "cursor_message_blocks")
    assert set(_catalog("cursor")["families"][2]["segments"][1]) == roles

    messages = []
    for role in sorted(roles):
        for combo in _subsets([STRING, *CURSOR_STORE_BLOCKS]):
            content = "prose" if combo == (STRING,) else [CURSOR_STORE_BLOCKS[name] for name in combo]
            messages.append({"role": role, "id": f"{role}-{len(messages)}", "content": content})
        messages.append({"role": role, "id": f"{role}-{len(messages)}", "content": []})
        messages.append({"role": role, "id": f"{role}-{len(messages)}"})
    assert len(messages) == 4 * 18
    store = _write_store(tmp_path / "every-combination", agent_id="agent-1", created_at_ms=1, updated_at_ms=2, messages=messages)

    decoded = decode_store_db(store)
    assert decoded.session is not None and decoded.diagnostics.message_count == len(messages)
    assert decoded.diagnostics.unknown_block_types == {}

    tool = _tool()
    observed = tool.census([store], "cursor")
    assert len([shape for shape in observed if shape.startswith("store/") and shape not in ("store/meta/0", "store/root")]) == 4 * 17
    assert tool.main(["check", "--provider", "cursor", str(store)]) == 0

    # A block only one reader has an arm for, and one neither has, are findings.
    odd = _write_store(
        tmp_path / "odd",
        agent_id="agent-2",
        created_at_ms=1,
        updated_at_ms=2,
        messages=[
            {"role": "assistant", "id": "1", "content": [{"type": "redacted-reasoning", "data": "opaque"}]},
            {"role": "assistant", "id": "2", "content": [{"type": "text", "text": "x"}, {"type": "image", "image": "opaque"}]},
            # A block with no type is not an empty message: neither reader has an arm for it.
            {"role": "assistant", "id": "3", "content": [{"text": "untyped"}]},
        ],
    )
    assert decode_store_db(odd).diagnostics.unknown_block_types == {"image": 1, "None": 1}
    catalog = _catalog("cursor")
    assert tool.unclassified(catalog, tool.census([odd], "cursor")) == [
        "store/assistant/image+text",
        "store/assistant/redacted-reasoning",
        "store/assistant/untyped",
    ]


# ---------------------------------------------------------------------------
# What a family means
# ---------------------------------------------------------------------------

FAMILY_CATALOG = {
    "provider": "example",
    "shapes": {
        "message/assistant/text": {"classification": "transcript:assistant", "keys": ["type", "content[].text"], "required_keys": ["type"]},
        "message/assistant/?": {"classification": "signal:error", "required_keys": ["type"]},
    },
    "families": [
        {
            "segments": [
                "message",
                {"assistant": {"keys": ["model"]}, "user": {"keys": []}},
                {
                    "text": {"classification": "transcript:text", "keys": ["content[].text"], "required_keys": ["content[].text"]},
                    "toolCall": {"classification": "provider_tool:call", "keys": ["content[].name"], "required_keys": ["content[].name"]},
                    "image": {"classification": "transcript:image", "required_keys": ["content[].data"]},
                },
            ],
            "keys": ["type", "id"],
            "required_keys": ["type"],
        }
    ],
}


def _observed(shape: str, *keys: str) -> dict:
    return {shape: {"count": 1, "keys": sorted(keys), "key_counts": {key: 1 for key in keys}}}


def test_a_family_classifies_every_combination_of_its_names_and_nothing_else():
    tool = _tool()
    for shape in ("message/assistant/text+toolCall", "message/user/toolCall", "message/assistant/image+text+toolCall"):
        assert tool.unclassified(FAMILY_CATALOG, _observed(shape)) == []
    for shape in (
        "message/assistant/text+video",
        "message/system/text",
        "message/assistant",
        "record/assistant/text",
        "message/assistant/text/extra",
    ):
        assert tool.unclassified(FAMILY_CATALOG, _observed(shape)) == [shape]


def test_a_family_row_is_the_union_of_what_its_names_carry():
    tool = _tool()
    row = tool.catalog_row(FAMILY_CATALOG, "message/assistant/text+toolCall")
    assert row == {
        "classification": "transcript:text + provider_tool:call",
        "keys": ["content[].name", "content[].text", "id", "model", "type"],
        "required_keys": ["content[].name", "content[].text", "type"],
    }

    shape = "message/assistant/text+toolCall"
    carried = ("type", "id", "model", "content[].text", "content[].name")
    assert tool.widened(FAMILY_CATALOG, _observed(shape, *carried)) == {}
    assert tool.drift(FAMILY_CATALOG, _observed(shape, *carried)) == {}
    assert tool.widened(FAMILY_CATALOG, _observed(shape, *carried, "brandNew")) == {shape: ["brandNew"]}
    # The tool call lost the key its arm reads.
    assert tool.drift(FAMILY_CATALOG, _observed(shape, "type", "content[].text")) == {shape: {"missing": {"content[].name": 1}, "lines": 1}}


def test_a_shape_with_no_recorded_keys_has_no_baseline_for_new_keys_but_keeps_its_required_ones():
    tool = _tool()
    # `image` was written from the parser's arm and never observed; so was the row for an empty message.
    for shape in ("message/user/image", "message/assistant/?"):
        assert tool.widened(FAMILY_CATALOG, _observed(shape, "type", "anything")) == {}
    assert tool.drift(FAMILY_CATALOG, _observed("message/user/image", "type")) == {
        "message/user/image": {"missing": {"content[].data": 1}, "lines": 1}
    }


def test_a_shapes_own_row_is_merged_with_its_family():
    tool = _tool()
    row = tool.catalog_row(FAMILY_CATALOG, "message/assistant/text")
    assert row["classification"] == "transcript:assistant"
    # `model` and `id` are known from the family, so they are not new on this row.
    assert row["keys"] == ["content[].text", "id", "model", "type"]
    assert row["required_keys"] == ["content[].text", "type"]


def test_seed_records_a_family_shape_as_seen_without_declassifying_it():
    tool = _tool()
    catalog = json.loads(json.dumps(FAMILY_CATALOG))
    seen = {
        "message/user/image": {
            "count": 1,
            "first_seen_version": "1.0.0",
            "last_seen_version": "1.0.0",
            "entrypoints": [],
            "keys": ["type", "content[].data"],
        },
        "novel": {"count": 1, "first_seen_version": None, "last_seen_version": None, "entrypoints": [], "keys": ["type"]},
    }
    assert tool.seed(catalog, seen) == (2, 0)
    assert catalog["shapes"]["message/user/image"]["classification"] == "transcript:image"
    assert catalog["shapes"]["novel"]["classification"] == "unclassified"
    # What it carried is now the baseline.
    assert tool.widened(catalog, _observed("message/user/image", "type", "content[].data", "later")) == {"message/user/image": ["later"]}


def test_a_key_two_blocks_of_one_line_carry_does_not_hide_a_line_without_it(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    lines = [
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text"}]}},
    ]
    session.write_text("".join(json.dumps(line) + "\n" for line in lines))
    tool = _tool()
    observed = tool.census([session], "pi")
    assert observed["message/user/text"]["key_counts"]["message.content[].text"] == 1
    assert tool.drift(_catalog("pi"), observed)["message/user/text"]["missing"] == {"message.content[].text": 1}
