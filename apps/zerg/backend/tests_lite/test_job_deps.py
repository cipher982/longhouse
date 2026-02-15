"""Tests for job pack dependency installation."""

from types import SimpleNamespace

from zerg.services.jobs_repo import install_jobs_deps


def test_install_job_deps_skips_missing(tmp_path):
    result = install_jobs_deps(tmp_path)
    assert result["installed"] is False
    assert result["skipped"] is True
    assert result["error"] is None


def test_install_job_deps_skips_empty(tmp_path):
    (tmp_path / "requirements.txt").write_text("   \n")
    result = install_jobs_deps(tmp_path)
    assert result["installed"] is False
    assert result["skipped"] is True
    assert result["error"] is None


def test_install_job_deps_hash_skip(tmp_path, monkeypatch):
    req_path = tmp_path / "requirements.txt"
    req_path.write_text("requests==2.32.0\n")

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    hash_path = tmp_path / "deps.sha"

    result_first = install_jobs_deps(tmp_path, hash_path=hash_path)
    assert result_first["installed"] is True
    assert result_first["skipped"] is False

    result_second = install_jobs_deps(tmp_path, hash_path=hash_path)
    assert result_second["installed"] is False
    assert result_second["skipped"] is True
    assert len(calls) == 1
