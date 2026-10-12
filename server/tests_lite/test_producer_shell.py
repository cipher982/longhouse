"""Every producer the factory runs is built in ``producer_shell``, and what it writes answers the factory's command.

One table over the producers, so the next one cannot drift: each is run through its real
command line with the arguments the factory passes, the shell's ``execute`` is replaced by
one that records the identity the producer gave it, and a passing verdict, a failing verdict
and a typed failure made from that identity go through ``result_envelope_failures``.
"""

from __future__ import annotations

import importlib
import json
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

_REPO = Path(__file__).resolve().parents[2]
_QA = _REPO / "server" / "zerg" / "qa"
_MANIFEST = {
    entry["module"]: entry["producer_id"] for entry in factory_registration.load_manifest(_REPO / "schemas" / "factory_producers.yml")
}
_ROWS = factory_registration.contract_rows(_REPO / "schemas" / "managed_providers.yml")
# The producers of a product subject are registered by the factory alone, so they are found by what they declare.
_SHELL_BUILT = sorted(f"zerg.qa.{path.stem}" for path in _QA.glob("*.py") if "\nPRODUCER = " in path.read_text(encoding="utf-8"))
# Registered producers still written by hand. This list only shrinks.
_NOT_YET_ON_THE_SHELL = frozenset(
    {
        "zerg.qa.claude_helm_lifecycle",
        "zerg.qa.claude_background_producer",
    }
)


def test_every_registered_producer_is_built_in_the_shell():
    assert set(_MANIFEST) - set(_SHELL_BUILT) == _NOT_YET_ON_THE_SHELL


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

    def execute(self, root, _body, **options):
        identities.append({name: options[name] for name in ("provider", "variant", "scenario_id") if name in options})
        return {"status": "fail"}

    monkeypatch.setattr(Producer, "execute", execute)
    commands = _commands(producer.registration)
    assert commands, f"{producer.registration.producer_id} covers no cell of the contract"
    for index, command in enumerate(commands):
        root = tmp_path / f"evidence-{index}"
        identities.clear()
        assert module.main(_factory_arguments(command, root, binary)) == 1, command
        assert len(identities) == 1, f"{module_name} did not run {command} through the shell once"
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
    blocked = _run_fixture(tmp_path, lambda run: run.block("prerequisite_unavailable", "nothing to ask"))
    assert (blocked["status"], blocked["failure_code"], blocked["assertions"]) == ("blocked", "prerequisite_unavailable", {})

    def body(_run: Run) -> None:
        raise RuntimeError("the caller types this")

    with pytest.raises(RuntimeError, match="the caller types this"):
        _FIXTURE.execute(tmp_path / "raises", body, raises=True)
    assert not (tmp_path / "raises" / "result.json").exists()
    # A producer never writes into another run's evidence.
    with pytest.raises(FileExistsError):
        _FIXTURE.execute(tmp_path / "raises", body)


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
