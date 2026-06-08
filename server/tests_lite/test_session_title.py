"""Unit tests for the pure timeline-title helpers."""

from zerg.services.session_title import anchor_freeze_policy
from zerg.services.session_title import freeze_anchor_title
from zerg.services.session_title import resolve_timeline_title
from zerg.services.session_title import sanitize_title
from zerg.services.session_title import structured_fallback_title


class TestSanitizeTitle:
    def test_none_and_empty(self):
        assert sanitize_title(None) is None
        assert sanitize_title("") is None
        assert sanitize_title("   \n  ") is None

    def test_strips_triple_quote_garbage(self):
        # The exact garbage-preview bug: a pasted prompt starting with fences.
        assert sanitize_title('"""\n\nplease fix the bug') == "please fix the bug"

    def test_strips_image_tags(self):
        assert sanitize_title("[Image #1] look at this screenshot") == "look at this screenshot"

    def test_strips_code_fences(self):
        text = "```python\nprint('x')\n```\nexplain this code"
        assert sanitize_title(text) == "explain this code"

    def test_strips_urls(self):
        out = sanitize_title("check https://example.com/very/long/path now")
        assert "http" not in out
        assert out.startswith("check")

    def test_keeps_markdown_link_label(self):
        assert sanitize_title("see [the docs](https://x.com) please") == "see the docs please"

    def test_strips_heading_marker(self):
        assert sanitize_title("## My Heading") == "My Heading"

    def test_word_budget_truncates_with_ellipsis(self):
        out = sanitize_title("one two three four five six seven eight nine ten")
        assert out == "one two three four five six seven eight…"

    def test_collapses_whitespace(self):
        assert sanitize_title("fix    the\t\tbug") == "fix the bug"

    def test_pure_garbage_returns_none(self):
        assert sanitize_title('"""') is None
        assert sanitize_title("[Image #1]") is None
        assert sanitize_title("```\ncode\n```") is None


class TestStructuredFallback:
    def test_project_and_branch(self):
        assert structured_fallback_title("zerg", "feat/x") == "zerg · feat/x"

    def test_project_only(self):
        assert structured_fallback_title("zerg", None) == "zerg"

    def test_nothing(self):
        assert structured_fallback_title(None, None) == "Untitled session"
        assert structured_fallback_title("  ", "") == "Untitled session"


class TestResolveTimelineTitle:
    def _resolve(self, **overrides):
        base = dict(
            anchor_title=None,
            summary_title=None,
            summary_status=None,
            first_user_message=None,
            project="zerg",
            git_branch="main",
        )
        base.update(overrides)
        return resolve_timeline_title(**base)

    def test_prefers_frozen_anchor(self):
        assert self._resolve(anchor_title="Fix Refresh Token", summary_title="Now Doing X") == "Fix Refresh Token"

    def test_anchor_wins_even_when_summary_drifts(self):
        # Muscle-memory property: the row does not move when summary_title changes.
        out = self._resolve(anchor_title="Refresh Token Rotation", summary_title="Completely Different Topic")
        assert out == "Refresh Token Rotation"

    def test_falls_to_ready_summary_when_no_anchor(self):
        assert self._resolve(summary_title="Debug Bedrock Race") == "Debug Bedrock Race"

    def test_falls_to_sanitized_first_message(self):
        out = self._resolve(first_user_message='"""\nhelp me debug this thing')
        assert out == "help me debug this thing"

    def test_first_message_beats_summarizing_placeholder(self):
        out = self._resolve(first_user_message="add a new endpoint", summary_status="pending")
        assert out == "add a new endpoint"

    def test_summarizing_placeholder_when_pending_and_no_message(self):
        assert self._resolve(summary_status="pending") == "Summarizing…"

    def test_structured_fallback_last(self):
        assert self._resolve() == "zerg · main"

    def test_never_freezes_garbage_via_anchor(self):
        # An anchor that sanitizes to nothing must fall through, not render blank.
        out = self._resolve(anchor_title='"""', summary_title="Real Title")
        assert out == "Real Title"


class TestFreezeAnchorTitle:
    def test_sanitizes_before_freezing(self):
        assert freeze_anchor_title('"""\nFix The Bug') == "Fix The Bug"

    def test_skips_when_nothing_usable(self):
        assert freeze_anchor_title("[Image #1]") is None
        assert freeze_anchor_title(None) is None


class TestAnchorFreezePolicy:
    def test_live_is_write_once(self):
        assert anchor_freeze_policy(is_closed=False) == "write_once"

    def test_closed_overwrites(self):
        assert anchor_freeze_policy(is_closed=True) == "overwrite"
