"""Regression coverage: tiktoken must never reject content as a 'disallowed
special token'.

Found live during the embeddings-v1 backfill: a session containing the
literal text ``<|endoftext|>`` made every encode() call in this module raise,
which the projector's error handling classified as transient (correct, since
it isn't a config error) but which was actually deterministic per session --
it would fail identically forever without ever succeeding on retry, wasting
projector cycles on a session that could never complete. tiktoken treats
special-token-looking substrings as unsafe by default; conversation text is
not code the model will execute, so there is nothing to protect against here
and it must always be treated as plain text.
"""

from __future__ import annotations

from zerg.services.session_processing.tokens import count_tokens
from zerg.services.session_processing.tokens import truncate

ADVERSARIAL_TEXT = "before <|endoftext|> middle <|fim_prefix|> after"


def test_count_tokens_does_not_raise_on_special_token_text():
    assert count_tokens(ADVERSARIAL_TEXT) > 0


def test_truncate_head_does_not_raise_on_special_token_text():
    truncated, count, was_truncated = truncate(ADVERSARIAL_TEXT, max_tokens=3, strategy="head")
    assert count <= 3
    assert was_truncated is True


def test_truncate_tail_does_not_raise_on_special_token_text():
    truncated, count, was_truncated = truncate(ADVERSARIAL_TEXT, max_tokens=3, strategy="tail")
    assert count <= 3
    assert was_truncated is True


def test_truncate_sandwich_does_not_raise_on_special_token_text():
    long_text = " ".join([ADVERSARIAL_TEXT] * 50)
    truncated, count, was_truncated = truncate(long_text, max_tokens=20, strategy="sandwich")
    assert count <= 20
    assert was_truncated is True
