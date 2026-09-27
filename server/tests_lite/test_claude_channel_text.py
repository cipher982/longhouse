from zerg.services.claude_channel_text import strip_claude_channel_wrapper


def test_strip_claude_channel_wrapper_handles_full_message_wrapper():
    raw = '<channel source="longhouse">\ncontinue the deploy\n</channel>'

    assert strip_claude_channel_wrapper(raw) == "continue the deploy"


def test_strip_claude_channel_wrapper_preserves_partial_or_inline_markup():
    assert strip_claude_channel_wrapper('prefix <channel source="longhouse">hello</channel>') == (
        'prefix <channel source="longhouse">hello</channel>'
    )
    assert strip_claude_channel_wrapper('<channel source="longhouse">unterminated') == ('<channel source="longhouse">unterminated')


def test_strip_claude_channel_wrapper_preserves_other_channel_sources():
    raw = '<channel source="other-server">\nkeep this content\n</channel>'

    assert strip_claude_channel_wrapper(raw) == raw
