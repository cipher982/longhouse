import pytest

from zerg.qa.runtime_target import HOSTED_TARGET_ENV
from zerg.qa.runtime_target import require_disposable_runtime


@pytest.mark.parametrize(
    "api_url",
    [None, "", "http://127.0.0.1:43210", "http://localhost:47300/api", "http://[::1]:8080"],
)
def test_local_runtime_hosts_are_allowed(api_url, monkeypatch) -> None:
    monkeypatch.delenv(HOSTED_TARGET_ENV, raising=False)

    require_disposable_runtime(api_url)


def test_hosted_runtime_host_is_refused_unless_named(monkeypatch) -> None:
    monkeypatch.delenv(HOSTED_TARGET_ENV, raising=False)

    with pytest.raises(RuntimeError, match=HOSTED_TARGET_ENV):
        require_disposable_runtime("https://someone.longhouse.ai")

    monkeypatch.setenv(HOSTED_TARGET_ENV, "https://canary.longhouse.ai/")
    require_disposable_runtime("https://canary.longhouse.ai")
    with pytest.raises(RuntimeError):
        require_disposable_runtime("https://someone.longhouse.ai")
