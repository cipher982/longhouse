from zerg.auth.redirects import normalize_local_return_to


def test_normalize_local_return_to_allows_local_absolute_paths():
    assert normalize_local_return_to("/timeline") == "/timeline"
    assert normalize_local_return_to("/loop/card/demo?view=compact") == "/loop/card/demo?view=compact"


def test_normalize_local_return_to_rejects_external_or_relative_targets():
    assert normalize_local_return_to(None) is None
    assert normalize_local_return_to("") is None
    assert normalize_local_return_to("timeline") is None
    assert normalize_local_return_to("//evil.test/path") is None
    assert normalize_local_return_to("https://evil.test/path") is None
