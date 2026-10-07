from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "ops" / "launch-readiness.py"
    spec = importlib.util.spec_from_file_location("launch_readiness", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_latest_run_by_workflow_keeps_newest_run_id():
    mod = _load_module()

    latest = mod.latest_run_by_workflow(
        [
            {"workflowName": "CI", "databaseId": 30111752592, "status": "completed", "conclusion": "cancelled"},
            {"workflowName": "CI", "databaseId": 30111753300, "status": "completed", "conclusion": "success"},
            {"workflowName": "Launch Gate", "databaseId": 12, "status": "in_progress"},
            {"workflowName": "CI", "databaseId": 11, "status": "completed"},
        ]
    )

    assert latest["Launch Gate"]["databaseId"] == 12
    assert latest["CI"]["databaseId"] == 30111753300


def test_live_surface_requires_build_commit(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"

    monkeypatch.setattr(
        mod,
        "fetch_json_url",
        lambda url: {"status": "ok", "build": {"commit": sha}},
    )

    check = mod.check_live_surface("demo", "https://example.test/api/health", sha)

    assert check.ok is True
    assert check.name == "live:demo"


def test_live_surface_fails_when_commit_differs(monkeypatch):
    mod = _load_module()

    monkeypatch.setattr(
        mod,
        "fetch_json_url",
        lambda url: {"status": "ok", "build": {"commit": "a1160df0704b72763ed8e5cf252d2fc2819b5e5b"}},
    )

    check = mod.check_live_surface("demo", "https://example.test/api/health", "3b403158")

    assert check.ok is False
    assert "a1160df0" in check.detail


def test_live_surface_rejects_truncated_build_commit(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"

    monkeypatch.setattr(
        mod,
        "fetch_json_url",
        lambda url: {"status": "ok", "build": {"commit": "3b403158"}},
    )

    check = mod.check_live_surface("demo", "https://example.test/api/health", sha)

    assert check.ok is False


def test_required_workflow_must_succeed(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    payload = [
        {
            "workflowName": "Launch Gate",
            "databaseId": 12,
            "status": "completed",
            "conclusion": "skipped",
            "headSha": sha,
            "url": "https://example.test/run/12",
        }
    ]

    monkeypatch.setattr(
        mod,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    checks = mod.check_workflows("cipher982/longhouse", sha, ("Launch Gate",))

    assert checks == [
        mod.Check(
            "workflow:Launch Gate",
            "failed",
            "run 12 completed/skipped https://example.test/run/12",
        )
    ]


def test_missing_required_workflow_includes_dispatch_hint(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"

    monkeypatch.setattr(
        mod,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    checks = mod.check_workflows("cipher982/longhouse", sha, ("Launch Gate",))

    assert checks == [
        mod.Check(
            "workflow:Launch Gate",
            "missing",
            "no exact-SHA run found",
            hint=(
                "No exact-SHA evidence exists for this required workflow. "
                "It may be path-filtered for this commit; dispatch it on a branch or tag "
                "that points at 3b4031587155: "
                "gh workflow run launch-gate.yml -R cipher982/longhouse --ref <branch-or-tag>"
            ),
        )
    ]


def test_workflow_dispatch_hint_files_exist():
    mod = _load_module()
    repo_root = Path(__file__).resolve().parents[2]

    for workflow_file in mod.WORKFLOW_DISPATCH_FILES.values():
        assert (repo_root / ".github" / "workflows" / workflow_file).exists()


def test_default_workflows_use_release_artifact_evidence():
    mod = _load_module()

    assert "Local Runtime Binary Release" in mod.DEFAULT_REQUIRED_WORKFLOWS
    assert "Installer Validation Ring" not in mod.DEFAULT_REQUIRED_WORKFLOWS


def test_human_output_prints_hints(capsys):
    mod = _load_module()

    mod.print_human([mod.Check("workflow:Launch Gate", "missing", "no exact-SHA run found", hint="dispatch it")])

    captured = capsys.readouterr()
    assert "FAIL workflow:Launch Gate: no exact-SHA run found" in captured.out
    assert "HINT dispatch it" in captured.out


def test_wait_mode_exits_on_terminal_workflow_failure(monkeypatch, capsys):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    checks = [
        mod.Check(
            "workflow:Launch Gate",
            "failed",
            "run 12 completed/failure https://example.test/run/12",
        )
    ]

    monkeypatch.setattr(mod, "resolve_sha", lambda root, rev: sha)
    monkeypatch.setattr(
        mod,
        "run_checks",
        lambda args, target, required, **kwargs: checks,
    )
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: (_ for _ in ()).throw(AssertionError("should not sleep")))

    rc = mod.main(["--sha", sha, "--wait", "--timeout", "600", "--poll", "30"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "failed terminal checks" in captured.err


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_mode_missing_workflow_fails_after_discovery_grace(monkeypatch, capsys):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    clock = _FakeClock()
    calls = 0

    def fake_checks(args, target, required, **kwargs):
        nonlocal calls
        calls += 1
        return [
            mod.Check(
                "workflow:Local Runtime Binary Release",
                "missing",
                "no exact-SHA run found",
                hint="dispatch it",
            )
        ]

    monkeypatch.setattr(mod, "resolve_sha", lambda root, rev: sha)
    monkeypatch.setattr(mod, "run_checks", fake_checks)
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)

    rc = mod.main(
        [
            "--sha",
            sha,
            "--wait",
            "--timeout",
            "600",
            "--poll",
            "30",
            "--discovery-grace",
            "60",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert calls == 3
    assert clock.now == 60
    assert captured.err.count("Launch readiness pending") == 1
    assert "failed terminal checks" in captured.err
    assert "after workflow discovery grace" in captured.out


def test_wait_mode_accepts_workflow_appearing_before_grace(monkeypatch, capsys):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    clock = _FakeClock()
    sequences = [
        mod.Check("workflow:CI", "missing", "no exact-SHA run found"),
        mod.Check("workflow:CI", "pending", "run 12 queued/-"),
        mod.Check("workflow:CI", "succeeded", "run 12 completed/success"),
    ]

    def fake_checks(args, target, required, **kwargs):
        return [sequences.pop(0)]

    monkeypatch.setattr(mod, "resolve_sha", lambda root, rev: sha)
    monkeypatch.setattr(mod, "run_checks", fake_checks)
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)

    rc = mod.main(
        [
            "--sha",
            sha,
            "--wait",
            "--timeout",
            "600",
            "--poll",
            "30",
            "--discovery-grace",
            "300",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert clock.now == 60
    assert captured.err.count("Launch readiness pending") == 2
    assert "workflow:CI=missing" in captured.err
    assert "workflow:CI=pending" in captured.err


def test_wait_mode_suppresses_unchanged_pending_output(monkeypatch, capsys):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    clock = _FakeClock()

    monkeypatch.setattr(mod, "resolve_sha", lambda root, rev: sha)
    monkeypatch.setattr(
        mod,
        "run_checks",
        lambda args, target, required, **kwargs: [mod.Check("workflow:CI", "pending", "run 12 in_progress/-")],
    )
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)

    rc = mod.main(["--sha", sha, "--wait", "--timeout", "5", "--poll", "1"])

    captured = capsys.readouterr()
    assert rc == 1
    assert clock.now == 5
    assert captured.err.count("Launch readiness pending") == 1


def test_run_checks_memoizes_successful_immutable_release_checks(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    calls = {"package": 0, "artifact": 0}
    args = SimpleNamespace(
        repo="cipher982/longhouse",
        skip_workflows=True,
        skip_live=True,
        skip_release=False,
        skip_public_package=False,
        skip_runtime_artifacts=False,
    )

    monkeypatch.setattr(
        mod,
        "check_latest_release",
        lambda repo, target: (
            mod.Check("release:latest", "succeeded", f"v0.1.30 commit={target}"),
            "v0.1.30",
        ),
    )
    monkeypatch.setattr(mod, "runtime_artifact_components", lambda: ("engine",))

    def package(tag, target):
        calls["package"] += 1
        return mod.Check("package:pypi", "succeeded", f"version=0.1.30 commit={target}")

    def artifact(root, tag, target, component):
        calls["artifact"] += 1
        return mod.Check(
            f"runtime-artifact:{component}",
            "succeeded",
            f"version=0.1.30 commit={target}",
        )

    monkeypatch.setattr(mod, "check_public_package", package)
    monkeypatch.setattr(mod, "check_runtime_artifact", artifact)
    cache = {}

    first = mod.run_checks(args, sha, (), immutable_success_cache=cache)
    second = mod.run_checks(args, sha, (), immutable_success_cache=cache)

    assert first == second
    assert calls == {"package": 1, "artifact": 1}


def test_check_states_cover_machine_readable_contract():
    mod = _load_module()

    checks = [
        mod.Check("missing", "missing", "none"),
        mod.Check("pending", "pending", "queued"),
        mod.Check("succeeded", "succeeded", "done"),
        mod.Check("failed", "failed", "bad"),
    ]

    assert [check.state for check in checks] == ["missing", "pending", "succeeded", "failed"]


def test_public_package_requires_version_and_commit(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    payload = {"build": {"version": "0.1.17", "commit": sha}}
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(mod, "run", fake_run)

    check = mod.check_public_package("v0.1.17", sha)

    assert check.ok is True
    assert "version=0.1.17" in check.detail
    assert "longhouse-server" in commands[0]
    assert "longhouse" not in commands[0]


def test_public_package_fails_on_stale_commit(monkeypatch):
    mod = _load_module()
    payload = {
        "build": {
            "version": "0.1.16",
            "commit": "a1160df0704b72763ed8e5cf252d2fc2819b5e5b",
        }
    }

    monkeypatch.setattr(
        mod,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    check = mod.check_public_package("v0.1.17", "3b403158")

    assert check.ok is False
    assert "a1160df0" in check.detail


def test_runtime_artifact_check_requires_exact_identity(monkeypatch, tmp_path):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"build_identity": {"version": "0.1.17", "commit": sha}}),
            stderr="",
        )

    monkeypatch.setattr(mod, "run", fake_run)

    check = mod.check_runtime_artifact(tmp_path, "v0.1.17", sha, "engine")

    assert check.ok is True
    assert "version=0.1.17" in check.detail
    cmd, kwargs = calls[0]
    assert "--python" in cmd
    assert "3.12" in cmd
    assert "longhouse==0.1.17" in cmd
    assert "--expected-build-commit" in cmd
    assert sha in cmd
    assert kwargs["env"]["HOME"]
    assert "LONGHOUSE_ENGINE_SOURCE" not in kwargs["env"]


def test_runtime_artifact_check_fails_on_missing_identity(monkeypatch, tmp_path):
    mod = _load_module()

    monkeypatch.setattr(
        mod,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({}), stderr=""),
    )

    check = mod.check_runtime_artifact(tmp_path, "v0.1.17", "3b403158", "engine")

    assert check.ok is False
    assert "missing build_identity" in check.detail


# --- coverage, not identity, on a busy main (v0.1.76, 2026-10-07) ----------------------------------------

CANDIDATE = "0bc8b44b55bb1da46204ce91201df462cff470a7"
NEWER = "e9e02b21b0000000000000000000000000000000"
UNRELATED = "f3ece996486b2b1d56325ce4cd62cf88197b9a48"


def _fake_gh(monkeypatch, mod, *, exact, main_runs=(), deploys=None, reruns=None):
    """Answers `gh run list --commit SHA` with `exact`, `gh run list --branch main` with `main_runs`,
    `--workflow "Deploy and Verify" --commit X` from `deploys`, and records `gh run rerun`."""

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "run", "rerun"]:
            if reruns is not None:
                reruns.append(cmd[3])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "--branch" in cmd:
            workflow = cmd[cmd.index("--workflow") + 1]
            return SimpleNamespace(returncode=0, stdout=json.dumps([r for r in main_runs if r["workflow"] == workflow]), stderr="")
        if "--workflow" in cmd and cmd[cmd.index("--workflow") + 1] == "Deploy and Verify":
            commit = cmd[cmd.index("--commit") + 1]
            return SimpleNamespace(returncode=0, stdout=json.dumps((deploys or {}).get(commit, [])), stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(exact), stderr="")

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "descends_from", lambda ancestor, commit: ancestor == CANDIDATE and commit == NEWER)


def _cancelled(workflow="CI", run_id=37650853995):
    return {
        "workflowName": workflow,
        "databaseId": run_id,
        "status": "completed",
        "conclusion": "cancelled",
        "headSha": CANDIDATE,
        "url": f"https://example.test/run/{run_id}",
    }


def test_a_superseded_run_is_covered_by_the_same_workflow_succeeding_on_a_descendant(monkeypatch):
    mod = _load_module()
    _fake_gh(
        monkeypatch,
        mod,
        exact=[_cancelled()],
        main_runs=[
            {"workflow": "CI", "databaseId": 2, "headSha": NEWER, "status": "completed", "conclusion": "success", "url": "u2"},
        ],
    )

    [check] = mod.check_workflows("cipher982/longhouse", CANDIDATE, ("CI",), accept_covering=True)

    assert check.ok
    assert "cancelled (superseded)" in check.detail and NEWER[:12] in check.detail


def test_a_superseded_run_waits_on_a_covering_run_still_in_progress(monkeypatch):
    mod = _load_module()
    _fake_gh(
        monkeypatch,
        mod,
        exact=[_cancelled()],
        main_runs=[
            {"workflow": "CI", "databaseId": 2, "headSha": NEWER, "status": "in_progress", "conclusion": None, "url": "u2"},
        ],
    )

    [check] = mod.check_workflows("cipher982/longhouse", CANDIDATE, ("CI",), accept_covering=True)

    assert check.state == "pending" and not check.terminal


def test_a_run_on_a_commit_that_does_not_contain_the_candidate_covers_nothing(monkeypatch):
    mod = _load_module()
    _fake_gh(
        monkeypatch,
        mod,
        exact=[_cancelled()],
        main_runs=[
            {"workflow": "CI", "databaseId": 2, "headSha": UNRELATED, "status": "completed", "conclusion": "success", "url": "u2"},
        ],
    )

    [check] = mod.check_workflows("cipher982/longhouse", CANDIDATE, ("CI",), accept_covering=True)

    assert check.terminal and check.rerun_id == "37650853995"


def test_a_genuine_failure_is_never_covered(monkeypatch):
    mod = _load_module()
    failed = {**_cancelled(), "conclusion": "failure"}
    _fake_gh(
        monkeypatch,
        mod,
        exact=[failed],
        main_runs=[
            {"workflow": "CI", "databaseId": 2, "headSha": NEWER, "status": "completed", "conclusion": "success", "url": "u2"},
        ],
    )

    [check] = mod.check_workflows("cipher982/longhouse", CANDIDATE, ("CI",), accept_covering=True)

    assert check.terminal and check.rerun_id is None


def test_without_accept_covering_a_cancelled_run_stays_a_failure(monkeypatch):
    mod = _load_module()
    _fake_gh(
        monkeypatch,
        mod,
        exact=[_cancelled()],
        main_runs=[
            {"workflow": "CI", "databaseId": 2, "headSha": NEWER, "status": "completed", "conclusion": "success", "url": "u2"},
        ],
    )

    [check] = mod.check_workflows("cipher982/longhouse", CANDIDATE, ("CI",))

    assert check.terminal and check.rerun_id is None


def test_the_canary_may_serve_a_descendant_whose_own_deploy_passed(monkeypatch):
    mod = _load_module()
    _fake_gh(
        monkeypatch,
        mod,
        exact=[],
        deploys={
            NEWER: [{"databaseId": 9, "status": "completed", "conclusion": "success", "url": "d9", "headSha": NEWER}],
        },
    )
    monkeypatch.setattr(mod, "fetch_json_url", lambda url: {"status": "ok", "build": {"commit": NEWER}})

    check = mod.check_live_surface("canary", "https://c.test/api/health", CANDIDATE, accept_covering=True, repo="r")

    assert check.ok and "serves descendant" in check.detail
    assert not mod.check_live_surface("canary", "https://c.test/api/health", CANDIDATE).ok  # identity without the flag


def test_the_canary_on_a_descendant_without_a_passed_deploy_or_on_an_unrelated_commit_is_pending(monkeypatch):
    mod = _load_module()
    _fake_gh(
        monkeypatch,
        mod,
        exact=[],
        deploys={
            NEWER: [{"databaseId": 9, "status": "in_progress", "conclusion": None, "url": "d9", "headSha": NEWER}],
        },
    )
    for served in (NEWER, UNRELATED):
        monkeypatch.setattr(mod, "fetch_json_url", lambda url, served=served: {"status": "ok", "build": {"commit": served}})
        check = mod.check_live_surface("canary", "https://c.test/api/health", CANDIDATE, accept_covering=True, repo="r")
        assert check.state == "pending"


def test_wait_mode_reruns_an_uncovered_superseded_run_a_bounded_number_of_times(monkeypatch, capsys):
    mod = _load_module()
    reruns: list[str] = []
    _fake_gh(monkeypatch, mod, exact=[_cancelled()], reruns=reruns)  # stays cancelled, nothing covers it
    monkeypatch.setattr(mod, "resolve_sha", lambda root, rev: CANDIDATE)
    clock = _FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)

    rc = mod.main(
        [
            "--sha",
            CANDIDATE,
            "--required-workflow",
            "CI",
            "--skip-live",
            "--skip-release",
            "--skip-public-package",
            "--skip-runtime-artifacts",
            "--accept-covering",
            "--redispatch-superseded",
            "2",
            "--wait",
            "--timeout",
            "7200",
            "--poll",
            "30",
        ]
    )

    err = capsys.readouterr().err
    assert reruns == ["37650853995", "37650853995"]
    assert "re-dispatched it for the exact SHA (2/2)" in err
    assert rc == 1 and "failed terminal checks" in err  # the third cancellation is terminal
    # Each rerun had its grace to register before the next one: not both back to back.
    assert clock.now >= 2 * mod.RERUN_REGISTER_GRACE_S
    assert clock.now < 7200


def test_wait_mode_passes_once_a_rerun_succeeds(monkeypatch, capsys):
    mod = _load_module()
    states = iter([_cancelled(), {**_cancelled(), "status": "queued", "conclusion": None}, {**_cancelled(), "conclusion": "success"}])
    current = {"run": next(states)}
    reruns: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "run", "rerun"]:
            reruns.append(cmd[3])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "--branch" in cmd:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        out = json.dumps([current["run"]])
        current["run"] = next(states, current["run"])
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "resolve_sha", lambda root, rev: CANDIDATE)
    clock = _FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)

    rc = mod.main(
        [
            "--sha",
            CANDIDATE,
            "--required-workflow",
            "CI",
            "--skip-live",
            "--skip-release",
            "--skip-public-package",
            "--skip-runtime-artifacts",
            "--accept-covering",
            "--redispatch-superseded",
            "2",
            "--wait",
            "--timeout",
            "7200",
            "--poll",
            "30",
        ]
    )

    assert rc == 0, capsys.readouterr().err
    assert reruns == ["37650853995"]
