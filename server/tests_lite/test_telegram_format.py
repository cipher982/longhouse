from zerg.services.telegram_format import format_for_telegram


def test_format_for_telegram_plain_text():
    assert format_for_telegram("Hello world") == "Hello world"


def test_format_for_telegram_basic_markdown():
    assert format_for_telegram("This is **bold**") == "This is <b>bold</b>"
    assert format_for_telegram("*italic*") == "<i>italic</i>"
    assert format_for_telegram("~~deleted~~") == "<s>deleted</s>"


def test_format_for_telegram_escapes_plain_text():
    assert format_for_telegram("A & B < C > D") == "A &amp; B &lt; C &gt; D"


def test_format_for_telegram_preserves_code_as_escaped_html():
    assert format_for_telegram("Run `a < b`") == "Run <code>a &lt; b</code>"
    assert format_for_telegram("```\na < b && b > c\n```") == "<pre>a &lt; b &amp;&amp; b &gt; c</pre>"
