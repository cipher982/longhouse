import pytest

from zerg.services.workspace_manager import (
    validate_branch_name,
    validate_git_repo_url,
    validate_run_id,
)


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
    ],
)
def test_validate_git_repo_url_valid(repo_url: str) -> None:
    validate_git_repo_url(repo_url)


@pytest.mark.parametrize(
    "repo_url",
    [
        "",
        "-oops",
        "file:///etc/passwd",
        "ssh:///missing-host",
        "ssh://-oProxyCommand=bad@github.com/repo.git",
        "git@github.com",
        "git@-oHost:repo.git",
    ],
)
def test_validate_git_repo_url_invalid(repo_url: str) -> None:
    with pytest.raises(ValueError):
        validate_git_repo_url(repo_url)


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "feature/test",
        "release-1.0",
        "hotfix.v1",
    ],
)
def test_validate_branch_name_valid(branch: str) -> None:
    validate_branch_name(branch)


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "-bad",
        ".bad",
        "bad..name",
        "bad.lock",
        "bad name",
    ],
)
def test_validate_branch_name_invalid(branch: str) -> None:
    with pytest.raises(ValueError):
        validate_branch_name(branch)


@pytest.mark.parametrize(
    "run_id",
    [
        "run_123-abc",
        "RUN_001",
    ],
)
def test_validate_run_id_valid(run_id: str) -> None:
    validate_run_id(run_id)


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "bad/run",
        "bad..id",
        "bad id",
    ],
)
def test_validate_run_id_invalid(run_id: str) -> None:
    with pytest.raises(ValueError):
        validate_run_id(run_id)
