from __future__ import annotations

import json

import pytest

from zerg.utils import fast_json

# Inputs msgspec rejects (or orjson would mis-decode) but json accepts; each
# must match json exactly.
_STDLIB_ONLY = [
    '{"x": NaN}',
    "[Infinity, -Infinity]",
    '"\\ud800"',  # lone surrogate
    "123456789012345678901234567890",  # beyond 64 bits
    '{"a": 1, "a": 2}',  # duplicate keys: last wins in both
    "{}".encode("utf-16"),  # UTF-16 bytes with BOM
    "-123456789012345678901234567890",
    "1e400",  # out-of-range float: json gives inf
]


@pytest.mark.parametrize(
    "data", _STDLIB_ONLY + ['{"type": "user", "message": {"content": "<command-name>/x</command-name>"}}', b'{"k": [1, 2.5, null, true]}']
)
def test_loads_matches_stdlib(data):
    expected = json.loads(data)
    actual = fast_json.loads(data)
    if isinstance(expected, float) or (isinstance(expected, list) and any(isinstance(v, float) and v != v for v in expected)):
        assert repr(actual) == repr(expected)
    else:
        assert actual == expected
        assert type(actual) is type(expected)


@pytest.mark.parametrize("data", ["", "{", "nope", b"\xff\xfe\x00"])
def test_invalid_input_raises_what_stdlib_raises(data):
    with pytest.raises(Exception) as stdlib_error:
        json.loads(data)
    with pytest.raises(type(stdlib_error.value)):
        fast_json.loads(data)


def test_command_prefilter_still_parses_escaped_markup():
    from zerg.services.provider_interaction_semantics import claude_sequence_dependent_control_candidate

    plain = '{"type": "user", "promptId": "p1", "message": {"role": "user", "content": "<command-name>/x</command-name>"}}'
    escaped = plain.replace("command-name", "command\\u002dname")
    assert "command-" not in escaped
    assert claude_sequence_dependent_control_candidate(content_text="", raw_json=plain) is True
    assert claude_sequence_dependent_control_candidate(content_text="", raw_json=escaped) is True
    assert (
        claude_sequence_dependent_control_candidate(
            content_text="", raw_json='{"type": "user", "promptId": "p1", "message": {"content": "hi"}}'
        )
        is False
    )
