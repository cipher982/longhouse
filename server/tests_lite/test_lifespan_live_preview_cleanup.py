from zerg.lifespan import _live_preview_cleanup_enabled


def test_live_preview_cleanup_is_opt_in(monkeypatch):
    monkeypatch.delenv("LONGHOUSE_ENABLE_LIVE_PREVIEW_CLEANUP", raising=False)

    assert _live_preview_cleanup_enabled() is False


def test_live_preview_cleanup_can_be_enabled(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ENABLE_LIVE_PREVIEW_CLEANUP", "true")

    assert _live_preview_cleanup_enabled() is True
