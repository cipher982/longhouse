"""Every producer the factory runs is built in ``producer_shell``, and what it writes answers the factory's command.

One table over the producers, so the next one cannot drift: each is run through its real
command line with the arguments the factory passes, the shell's ``execute`` is replaced by
one that records the identity the producer gave it, and a passing verdict, a failing verdict
and a typed failure made from that identity go through ``result_envelope_failures``.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import re
from pathlib import Path

import pytest

from zerg.qa import factory_registration
from zerg.qa import producer_shell
from zerg.qa.live_kit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_kit import RUNTIME_API_URL_ENV
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import ProducerFailure
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import expired

_REPO = Path(__file__).resolve().parents[2]
_QA = _REPO / "server" / "zerg" / "qa"
_MANIFEST = {
    entry["module"]: entry["producer_id"] for entry in factory_registration.load_manifest(_REPO / "schemas" / "factory_producers.yml")
}
_ROWS = factory_registration.contract_rows(_REPO / "schemas" / "managed_providers.yml")
# The producers of a product subject are registered by the factory alone, so they are found by what they declare.
_SHELL_BUILT = sorted(f"zerg.qa.{path.stem}" for path in _QA.glob("*.py") if "\nPRODUCER = " in path.read_text(encoding="utf-8"))


def test_every_registered_producer_is_built_in_the_shell():
    assert set(_MANIFEST) <= set(_SHELL_BUILT)
    # The factory's envelope has no blocked status, so none of its producers may end a run that way.
    assert [name for name in _MANIFEST if importlib.import_module(name).PRODUCER.may_block] == []


def test_a_revision_is_a_number_only_where_a_registration_declares_it():
    # A result, or a receipt embedded in one, that writes its own number drifts from the registration at the next bump.
    literal: list[str] = []
    for path in sorted(_QA.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        declared = {
            id(node)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "ProducerRegistration"
            for node in ast.walk(call)
        }
        written = [keyword.value for keyword in ast.walk(tree) if isinstance(keyword, ast.keyword) and keyword.arg == "scenario_revision"]
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                written += [value for key, value in zip(node.keys, node.values) if getattr(key, "value", None) == "scenario_revision"]
        literal += [
            f"{path.name}:{value.lineno}"
            for value in written
            if id(value) not in declared and any(isinstance(leaf, ast.Constant) and type(leaf.value) is int for leaf in ast.walk(value))
        ]
    assert not literal, f"a scenario revision written as a number outside a registration: {literal}"


def _commands(registration: ProducerRegistration) -> list[dict]:
    if registration.providers:
        return factory_registration.factory_commands(registration.to_dict(), _ROWS)
    # A product subject has no provider rows: the factory runs one command per assertion, with no provider and no variant.
    return [
        {
            "producer_id": registration.producer_id,
            "provider": None,
            "assertion_id": assertion_id,
            "variant": variant,
            "execution_variant": None,
            "scenario_id": registration.scenario_id,
            "scenario_revision": registration.scenario_revision,
            "evidence_class": registration.evidence_classes[0],
            "observation_scope": registration.observation_scope,
        }
        for assertion_id, variant in registration.assertion_cells
    ]


def _factory_arguments(command: dict, root: Path, binary: Path) -> list[str]:
    """The arguments the factory passes for ``command`` (control-plane ``_provider_producer_argv``), as one envelope."""

    arguments = ["--evidence-root", str(root), "--repo-root", str(_REPO), "--engine", str(binary), "--provider-version", "0.0.0"]
    arguments += ["--longhouse-cli", str(binary), "--provider-bin", str(binary), "--model", "fixture-model"]
    if command["execution_variant"] is not None:
        arguments += ["--variant", command["execution_variant"]]
    if command["provider"] is not None:
        arguments += ["--provider", command["provider"], "--platform", "linux", "--architecture", "x86_64"]
        arguments += ["--evidence-class", command["evidence_class"], "--credential-binding", "runtime_host_control"]
    return arguments


def _observation_that_reached(producer: Producer, assertion_id: str) -> dict:
    """An observation in which the step behind ``assertion_id`` was reached, for a producer that declares what proves it."""

    observation: dict = {"reached": True}
    for marker in (producer.reached or {}).get(assertion_id, ()):
        *parents, leaf = marker.split(".")
        node = observation
        for parent in parents:
            node = node.setdefault(parent, {})
        node[leaf] = {"decided": True}
    return observation


@pytest.mark.parametrize("module_name", _SHELL_BUILT)
def test_a_producer_answers_each_factory_command_with_an_envelope_the_factory_accepts(module_name, monkeypatch, tmp_path, capsys):
    module = importlib.import_module(module_name)
    producer: Producer = module.PRODUCER
    assert producer.registration is module.REGISTRATION
    assert module.main(["--registration"]) == 0
    assert json.loads(capsys.readouterr().out) == producer.registration.to_dict()

    binary = tmp_path / "binary"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv(RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(RUNTIME_AGENTS_TOKEN_ENV, "fixture-token")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    monkeypatch.setenv("CODEX_API_KEY", "fixture-provider-token")
    identities: list[dict] = []
    evidence_classes: list[str] = []

    def execute(self, root, _body, **options):
        identities.append({name: options[name] for name in ("provider", "variant", "scenario_id") if name in options})
        evidence_classes.append(self.envelope(**identities[-1])["evidence_class"])
        return {"status": "fail"}

    monkeypatch.setattr(Producer, "execute", execute)
    commands = _commands(producer.registration)
    assert commands, f"{producer.registration.producer_id} covers no cell of the contract"
    for index, command in enumerate(commands):
        root = tmp_path / f"evidence-{index}"
        identities.clear()
        assert module.main(_factory_arguments(command, root, binary)) == 1, command
        assert len(identities) == 1, f"{module_name} did not run {command} through the shell once"
        # The result is the evidence class the factory asked for, not whichever the registration lists first.
        assert evidence_classes[-1] == command["evidence_class"]
        assertion_id = command["assertion_id"]
        observed = _observation_that_reached(producer, assertion_id)
        results = {
            "pass": producer.verdict(Run(root, observation=observed, assertions={assertion_id: True}), **identities[0]),
            "fail": producer.verdict(Run(root, observation=observed, assertions={assertion_id: False}), **identities[0]),
            "typed failure": producer.failure(Run(root), RuntimeError("synthetic"), **identities[0]),
            "failure after a failing verdict": producer.failure(
                Run(root, observation=observed, assertions={assertion_id: False}), RuntimeError("synthetic"), **identities[0]
            ),
        }
        assert results["pass"]["status"] == "pass" and results["fail"]["status"] == "fail"
        assert results["failure after a failing verdict"]["assertions"] == {assertion_id: False}
        problems = {
            kind: failures
            for kind, result in results.items()
            if (failures := factory_registration.result_envelope_failures(command, result))
        }
        assert not problems, f"the factory would refuse {producer.registration.producer_id}'s result for {command}: {problems}"


_REGISTRATION = ProducerRegistration(
    producer_id="fixture.shell.v1",
    producer_revision=1,
    scenario_id="fixture_shell",
    scenario_revision=3,
    assertion_cells=(("held", None),),
    providers=("codex",),
    platforms=("linux",),
    architectures=("x86_64",),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=("held",),
    acquisition_methods=("staged_release",),
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(),
    required_cleanup=(),
    implementation="server/tests_lite/test_producer_shell.py",
    oracle_source="server/tests_lite/test_producer_shell.py",
    oracle_entrypoint="held",
    executable_module="tests_lite.test_producer_shell",
)
_FIXTURE = Producer(_REGISTRATION, artifact_kind="fixture_result", failure_code="fixture_failed", needs=("evidence_root", "engine"))


def _run_fixture(tmp_path, body, **options) -> dict:
    result = _FIXTURE.execute(tmp_path / "evidence", body, **options)
    assert json.loads((tmp_path / "evidence" / "result.json").read_text(encoding="utf-8")) == json.loads(json.dumps(result, default=str))
    return result


def test_status_is_the_assertion_maps_and_an_empty_map_is_not_a_pass(tmp_path):
    def body(run: Run) -> None:
        run.observation = {}

    assert _run_fixture(tmp_path, body)["status"] == "fail"


@pytest.mark.parametrize(
    ("reached", "kept"),
    [
        ({"held": False}, True),  # a failing verdict the run reached is the answer
        ({"held": True}, False),  # every assertion held and then the run raised: the harness failed, not the subject
        ({}, False),
    ],
)
def test_a_run_that_raises_keeps_only_a_failing_verdict(tmp_path, reached, kept):
    def body(run: Run) -> None:
        run.write("receipt.json", {"reached": True})
        run.observation = {"seen": 1}
        run.assertions = dict(reached)
        run.fields["provider_binary"] = {"version": "1"}
        try:
            raise ProducerFailure("cleanup_unverified", "the provider outlived the run")
        finally:
            run.write("written-while-unwinding.json", {})

    result = _run_fixture(tmp_path, body, failure_cleanup={"status": "fail"})

    assert result["status"] == "fail" and result["failure_code"] == "cleanup_unverified"
    assert result["error"] == "ProducerFailure: the provider outlived the run"
    assert result["provider_binary"] == {"version": "1"}
    assert ("assertions" in result, "observation" in result) == (kept, kept)
    assert kept or result["partial_observation"] == {"seen": 1}
    assert factory_registration.typed_harness_failure(result) is not kept
    # The result is made after the run has unwound, so its manifest binds what cleanup wrote on the way out.
    assert {entry["path"] for entry in result["artifact_manifest"]} == {
        "receipt.json",
        "written-while-unwinding.json",
        "cleanup-receipt.json",
    }
    assert json.loads((tmp_path / "evidence" / "cleanup-receipt.json").read_text(encoding="utf-8")) == {
        "status": "fail",
        "error_type": "ProducerFailure",
    }


def test_a_run_stopped_by_an_expired_wait_says_what_it_waited_on_wrapped_or_not(tmp_path):
    def body(_run: Run) -> None:
        try:
            raise expired(RuntimeError("timed out waiting for the reply"), PROVIDER_TURN)
        except RuntimeError as exc:
            # A producer that adds what it last saw re-raises from the wait.
            raise RuntimeError(f"{exc}; last_observation={{}}") from exc

    result = _run_fixture(tmp_path, body)

    assert result["waited_on"] == "provider_turn"
    assert result["error"].startswith("RuntimeError: timed out waiting for the reply")


def test_a_failed_run_of_several_steps_reports_verdicts_only_for_the_steps_it_reached(tmp_path):
    producer = Producer(
        _REGISTRATION, artifact_kind="fixture_result", failure_code="fixture_failed", reached={"held": ("send",), "later": ("steer",)}
    )

    def body(run: Run) -> None:
        run.observation = {"send": {"marker_count": 0}}
        run.assertions = {"held": False, "later": False}  # the second step never ran: its False is not a finding
        raise RuntimeError("the steer step raised")

    result = producer.execute(tmp_path / "evidence", body)

    assert result["assertions"] == {"held": False} and result["failure_code"] == "fixture_failed"


def test_a_negative_control_reports_its_own_verdict_and_stays_inconclusive_evidence_when_it_fails(tmp_path):
    def caught(run: Run) -> None:
        run.observation = {"send": {"dropped": True}}
        run.assertions = {"held": False}
        run.control({"fault": "send_noop", "verdict": "rejected"}, status="pass")

    def failed(run: Run) -> None:
        caught(run)
        run.control({"fault": "send_noop", "verdict": "inconclusive"}, status="fail")
        run.failed = RuntimeError("the control could not finish")  # caught and cleaned up by the run itself

    result = _FIXTURE.execute(tmp_path / "caught", caught)
    assert (result["status"], result["assertions"]) == ("pass", {"held": False})  # the fault was caught

    # Reported like a run that raised, even to a caller that asked for the exception, and it keeps what it observed.
    result = _FIXTURE.execute(tmp_path / "failed", failed, raises=True)
    assert (result["status"], result["error"]) == ("fail", "RuntimeError: the control could not finish")
    assert result["observation"] == {"send": {"dropped": True}} and result["negative_control"]["verdict"] == "inconclusive"


def test_a_pass_whose_result_cannot_be_written_leaves_a_typed_failure_and_never_a_pass(tmp_path, monkeypatch):
    real_write, writes = producer_shell.write_json, []

    def write(path, payload):
        writes.append(payload.get("status"))
        if len(writes) == 1:
            raise OSError("filesystem race")
        real_write(path, payload)

    monkeypatch.setattr(producer_shell, "write_json", write)

    def body(run: Run) -> None:
        run.observation = {"seen": 1}
        run.assertions = {"held": True}

    result = _FIXTURE.execute(tmp_path / "evidence", body)

    assert writes == ["pass", "fail"]
    assert (result["status"], result["failure_code"], result["error"]) == ("fail", "fixture_failed", "OSError: filesystem race")
    assert json.loads((tmp_path / "evidence" / "result.json").read_text(encoding="utf-8"))["status"] == "fail"


def test_a_blocked_run_says_so_and_a_raising_caller_gets_the_exception_and_no_artifact(tmp_path):
    may_block = dataclasses.replace(_FIXTURE, may_block=True)
    blocked = may_block.execute(tmp_path / "blocked", lambda run: run.block("prerequisite_unavailable", "nothing to ask"))
    assert (blocked["status"], blocked["failure_code"], blocked["assertions"]) == ("blocked", "prerequisite_unavailable", {})
    # A producer that did not declare it has no such ending: the call is refused where it is made.
    refused = _run_fixture(tmp_path, lambda run: run.block("prerequisite_unavailable", "nothing to ask"))
    assert (refused["status"], refused["failure_code"]) == ("fail", "fixture_failed")
    assert refused["error"].startswith("ValueError: this producer does not declare may_block")

    def body(_run: Run) -> None:
        raise RuntimeError("the caller types this")

    with pytest.raises(RuntimeError, match="the caller types this"):
        _FIXTURE.execute(tmp_path / "raises", body, raises=True)
    assert not (tmp_path / "raises" / "result.json").exists()
    # A producer never writes into another run's evidence.
    with pytest.raises(FileExistsError):
        _FIXTURE.execute(tmp_path / "raises", body)


@pytest.mark.parametrize("name", sorted(producer_shell._SHELL_FIELDS))
def test_a_further_field_cannot_be_one_the_shell_reports(tmp_path, name):
    # A definition error, raised where the producer writes it: a colliding field would replace the verdict in the result.
    run = Run(tmp_path)
    for write in (
        lambda: run.fields.__setitem__(name, "pass"),
        lambda: run.fields.update({name: "pass"}),
        lambda: run.fields.update(**{name: "pass"}),
        lambda: run.fields.setdefault(name, "pass"),
        lambda: run.fields.__ior__({name: "pass"}),
        lambda: Run(tmp_path, fields={name: "pass"}),
    ):
        with pytest.raises(ValueError, match=f"{name} is the shell's to report"):
            write()
    assert run.fields == {}
    # Every other name is written the way a dict is.
    assert run.fields.setdefault("session_id", "first") == run.fields.setdefault("session_id", "second") == "first"
    run.fields |= {"code": "c"}
    assert run.fields == {"session_id": "first", "code": "c"} and type(run.fields).__name__ == "_Fields"
    run.fields.clear()
    # The code a failing verdict names is the producer's to say, and a status its assertions do not make is said outright.
    run.fields.update(failure_code="fixture_step_failed", session_id="s")
    run.assertions, run.status = {"held": True}, "fail"
    result = _FIXTURE.verdict(run)
    assert (result["status"], result["failure_code"], result["assertions"]) == ("fail", "fixture_step_failed", {"held": True})


def test_the_shell_reserves_every_field_it_reports_of_every_run_and_leaves_open_the_two_a_failure_adds(tmp_path):
    scenario = Producer(
        dataclasses.replace(_REGISTRATION, observation_scope="scenario"), artifact_kind="fixture_result", failure_code="fixture_failed"
    )
    for producer in (_FIXTURE, scenario):
        verdict = producer.verdict(Run(tmp_path))
        failure = producer.failure(Run(tmp_path), RuntimeError("synthetic"))
        assert set(verdict) <= producer_shell._SHELL_FIELDS
        # A run that raised is also given a code and an error, and those two are the producer's to word.
        assert set(failure) - producer_shell._SHELL_FIELDS == {"failure_code", "error"}
    worded = Run(tmp_path, fields={"failure_code": "fixture_step_failed", "error": "the step said why"})
    failure = _FIXTURE.failure(worded, RuntimeError("synthetic"))
    assert (failure["failure_code"], failure["error"]) == ("fixture_step_failed", "the step said why")


def test_a_result_that_is_not_the_producers_usual_kind_is_named_on_the_run(tmp_path):
    # A negative control's result has its own kind, on a verdict and on a failure of the control alike.
    run = Run(tmp_path)
    assert _FIXTURE.verdict(run)["artifact_kind"] == _FIXTURE.failure(run, RuntimeError("synthetic"))["artifact_kind"] == "fixture_result"
    run.artifact_kind = "negative_control_result"
    run.control({"status": "pass"}, status="pass")
    assert (
        _FIXTURE.verdict(run)["artifact_kind"]
        == _FIXTURE.failure(run, RuntimeError("synthetic"))["artifact_kind"]
        == "negative_control_result"
    )
    assert list(_FIXTURE.verdict(run))[:2] == ["schema_version", "artifact_kind"]


def test_no_producer_writes_a_shell_field_as_a_further_field():
    # The guard raises where such a line runs, which for a negative control is a live run. This finds the line unrun.
    names = "|".join(sorted(producer_shell._SHELL_FIELDS))
    written = re.compile(
        rf"""fields\[["'](?:{names})["']\]\s*=[^=]|fields\.(?:update|setdefault)\((?:[^)]*\b(?:{names})\s*=|\s*["'](?:{names})["'])"""
    )
    found = [
        f"{path.name}:{number}"
        for path in sorted(_QA.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if written.search(line)
    ]
    assert not found, f"a field the shell reports is written as a further field: {found}"


def test_a_result_carries_the_evidence_class_the_command_line_named(tmp_path, capsys):
    two_classes = dataclasses.replace(_REGISTRATION, evidence_classes=("live_token", "hermetic"))
    producer = Producer(two_classes, artifact_kind="fixture_result", failure_code="fixture_failed")

    def run(root: Path, *extra: str) -> tuple[int, dict]:
        code = producer.main(
            lambda args: producer.execute(args.evidence_root, lambda _run: None), ["--variant", "v", "--evidence-root", str(root), *extra]
        )
        return code, json.loads(capsys.readouterr().out)

    assert run(tmp_path / "named", "--evidence-class", "hermetic")[1]["evidence_class"] == "hermetic"
    # A caller that names none (the release-identity lane, a test) gets the registration's first.
    assert run(tmp_path / "unnamed")[1]["evidence_class"] == "live_token"
    assert run(tmp_path / "unlisted", "--evidence-class", "live_no_token") == (
        2,
        {"status": "fail", "failure_code": "evidence_class_not_registered"},
    )
    assert not (tmp_path / "unlisted").exists()


def test_a_producer_that_needs_an_argument_its_command_line_lacks_is_refused_when_it_is_defined():
    # A definition error, caught where the producer is built: every producer module builds its own at import.
    with pytest.raises(
        ValueError, match=r"fixture\.shell\.v1 needs arguments its command line does not define: \['codex_bin', 'no_such_argument'\]"
    ):
        Producer(
            _REGISTRATION,
            artifact_kind="k",
            failure_code="f",
            needs=("evidence_root", "no_such_argument"),
            binaries=(("codex_bin", "codex_binary_missing"),),
        )


def test_a_producer_answers_to_the_factorys_name_for_its_binary_and_to_its_historical_one():
    codex = Producer(
        _REGISTRATION, artifact_kind="k", failure_code="f", provider_bin_aliases=("--codex-bin",), provider_bin_dest="codex_bin"
    )

    for flag in ("--provider-bin", "--codex-bin"):
        assert codex.parser().parse_args(["--variant", "v", flag, "/bin/provider"]).codex_bin == Path("/bin/provider")


@pytest.mark.parametrize(
    ("arguments", "refused"),
    [
        (["--variant", "v"], "missing_required_argument:--evidence-root"),
        (["--variant", "v", "--evidence-root", "{root}"], "missing_required_argument:--engine"),
        (["--variant", "v", "--evidence-root", "{root}", "--engine", "{root}/absent"], "engine_binary_missing"),
        (["--variant", "v", "--evidence-root", "{root}", "--engine", "/bin/sh", "--model", "refused"], "model_refused"),
    ],
)
def test_the_command_line_refuses_before_anything_is_written(tmp_path, capsys, arguments, refused):
    producer = Producer(
        _REGISTRATION,
        artifact_kind="fixture_result",
        failure_code="fixture_failed",
        needs=("evidence_root", "engine"),
        binaries=(("engine", "engine_binary_missing"),),
        refuse=lambda args: "model_refused" if args.model == "refused" else None,
    )
    root = tmp_path / "evidence"

    assert producer.main(lambda _args: pytest.fail("a refused command ran"), [item.format(root=root) for item in arguments]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "fail", "failure_code": refused}
    assert not root.exists()
    # Asking for the registration never runs the producer, whatever else is on the command line.
    assert producer.main(lambda _args: pytest.fail("ran"), ["--registration", "--evidence-root", str(root)]) == 0


@pytest.mark.parametrize(
    ("url", "token"),
    [(None, "fixture-token"), ("http://127.0.0.1:9", None), (None, None), ("", "fixture-token"), ("http://127.0.0.1:9", "")],
)
def test_a_runtime_host_producer_without_its_control_credentials_is_refused_before_it_runs(tmp_path, capsys, monkeypatch, url, token):
    producer = Producer(
        _REGISTRATION, artifact_kind="fixture_result", failure_code="fixture_failed", needs=("evidence_root",), runtime_host=True
    )
    root = tmp_path / "evidence"
    arguments = ["--variant", "v", "--evidence-root", str(root)]
    for name, value in ((RUNTIME_API_URL_ENV, url), (RUNTIME_AGENTS_TOKEN_ENV, token)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)

    assert producer.main(lambda _args: pytest.fail("a refused command ran"), arguments) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}
    assert not root.exists()
    # Named on the command line they are credentials like any other, and a producer that drives no Runtime Host asks for none.
    ran: list[tuple[str, str]] = []
    named = [*arguments, "--api-url", "http://127.0.0.1:9", "--agents-token", "fixture-token"]
    assert producer.main(lambda args: ran.append((args.api_url, args.agents_token)) or {"status": "pass"}, named) == 0
    assert ran == [("http://127.0.0.1:9", "fixture-token")]
    hostless = Producer(_REGISTRATION, artifact_kind="fixture_result", failure_code="fixture_failed", needs=("evidence_root",))
    assert hostless.main(lambda _args: {"status": "pass"}, arguments) == 0


def test_a_runtime_host_producer_pointed_at_a_host_someone_uses_is_refused_with_a_typed_code(tmp_path, capsys, monkeypatch):
    from zerg.qa.live_kit import HOSTED_TARGET_ENV

    producer = Producer(
        _REGISTRATION, artifact_kind="fixture_result", failure_code="fixture_failed", needs=("evidence_root",), runtime_host=True
    )
    root = tmp_path / "evidence"
    hosted = "https://someone.longhouse.example"
    arguments = ["--variant", "v", "--evidence-root", str(root), "--api-url", hosted, "--agents-token", "fixture-token"]
    monkeypatch.delenv(HOSTED_TARGET_ENV, raising=False)

    assert producer.main(lambda _args: pytest.fail("a refused command ran"), arguments) == 2
    refused = json.loads(capsys.readouterr().out)
    assert (refused["status"], refused["failure_code"]) == ("fail", "runtime_host_not_disposable")
    # The refusal says which host and how to name it when it is meant.
    assert hosted in refused["error"] and HOSTED_TARGET_ENV in refused["error"]
    assert not root.exists()
    # Without its token the same host is still refused as a host, before anything is asked of it.
    assert producer.main(lambda _args: pytest.fail("a refused command ran"), arguments[:-2]) == 2
    assert json.loads(capsys.readouterr().out)["failure_code"] == "runtime_host_not_disposable"
    # Named as the target, it runs.
    monkeypatch.setenv(HOSTED_TARGET_ENV, hosted)
    assert producer.main(lambda _args: {"status": "pass"}, arguments) == 0
