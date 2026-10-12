"""The shell every producer the provider factory runs is built in.

A producer drives a real provider or a product surface through Longhouse and
answers one question per assertion. Around that work every producer does the
same things: prints its registration, parses the arguments the factory passes,
refuses a missing binary, builds the result the factory compares with its
command, turns an exception into a typed failure, writes ``result.json`` and
exits by the verdict. Each used to write that out by hand, twice (once for the
verdict and once for the failure), and the copies drifted: three status rules,
failure envelopes that forgot their scope, producers that left no artifact at
all when they raised.

This module is that shell, once. A producer keeps its module, its
``REGISTRATION`` and the work itself::

    PRODUCER = Producer(REGISTRATION, artifact_kind="...", failure_code="..._failed")

    def body(run: Run, args: argparse.Namespace) -> None:
        ...                               # drive the subject, write receipts with run.write
        run.observation = {...}
        run.assertions = {ASSERTION_ID: held}

    def run_scenario(args: argparse.Namespace) -> dict:
        return PRODUCER.execute(args.evidence_root, lambda run: body(run, args))

    def main(argv=None) -> int:
        return PRODUCER.main(run_scenario, argv)

The rules it holds for all of them:

- **Status.** ``pass`` when every assertion holds and ``fail`` otherwise, which
  is the rule the factory checks a result against. A producer does not compute
  a status of its own.
- **A failure keeps a verdict only when it reached one that fails**
  (``failed_results.settle_failed_result``). An exception after every
  assertion held, or before any was reached, is a typed harness failure with
  what was observed kept as ``partial_observation``.
- **Scope.** A scenario-scoped registration's every result says so, failed
  ones included.
- **What a wait was waiting on** and **a model's deviation** travel on the
  failure from the exception that carries them (``wait_kind``,
  ``model_compliance``).

It imports nothing from the producers, so any of them can import it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.qa.failed_results import settle_failed_result
from zerg.qa.live_kit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_kit import RUNTIME_API_URL_ENV
from zerg.qa.live_kit import require_disposable_runtime
from zerg.qa.model_compliance import failure_claim
from zerg.qa.provider_artifacts import artifact_manifest
from zerg.qa.provider_artifacts import now
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.wait_kind import failure_fields


def write_json(path: Path, payload: object) -> None:
    """Write ``payload`` as the JSON file at ``path``, whole or not at all."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def refusal(failure_code: str) -> int:
    """Refuse to run: print the reason as the factory reads it, and exit 2."""

    print(json.dumps({"status": "fail", "failure_code": failure_code}))
    return 2


class ProducerFailure(RuntimeError):
    """A failure outside the assertions that has a code of its own (a cleanup that did not verify, a refused precondition)."""

    def __init__(self, failure_code: str, message: str) -> None:
        self.failure_code = failure_code
        super().__init__(message)


@dataclass
class Run:
    """What one invocation has reached so far; the shell makes the result from it.

    ``observation`` and ``assertions`` are set as the work reaches them, so a
    failure later in the run keeps the verdict it had already reached.
    ``fields`` are further top-level fields of the result, on a verdict and on
    a failure alike (the provider binary's receipt, a session id).
    """

    root: Path
    observation: dict[str, Any] | None = None
    assertions: dict[str, bool] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)

    def write(self, name: str, payload: object) -> None:
        write_json(self.root / name, payload)

    def block(self, failure_code: str, error: str) -> None:
        """Say the run could not ask its question: a prerequisite was absent, which is neither a pass nor a failing verdict."""

        self.assertions = {}
        self.fields.update(status="blocked", failure_code=failure_code, error=error)


@dataclass(frozen=True)
class Producer:
    """One producer's registration, what it calls its result, and its command line.

    The command line is the envelope the factory passes (``parser``). ``needs``
    are the arguments this producer cannot run without, ``binaries`` the
    arguments that must name an executable file with the code each is refused
    with, and ``runtime_host`` says it drives a Runtime Host and so needs the
    host's URL and token, from ``--api-url`` and ``--agents-token`` or the
    environment. ``refuse`` is the producer's own precondition on the parsed
    arguments: the code to refuse them with, or None.
    """

    registration: ProducerRegistration
    artifact_kind: str
    # The code of a failure the producer did not name: a string, or the code for a given exception.
    failure_code: str | Callable[[BaseException], str]
    needs: tuple[str, ...] = ("evidence_root",)
    binaries: tuple[tuple[str, str], ...] = ()
    runtime_host: bool = False
    variants: tuple[str, ...] | None = None
    variant_required: bool = True
    provider_bin_aliases: tuple[str, ...] = ()
    # The environment variable ``--model`` defaults to, and the faults ``--negative-control`` may name.
    model_env: str | None = None
    negative_controls: tuple[str, ...] = ()
    refuse: Callable[[argparse.Namespace], str | None] | None = None
    add_arguments: Callable[[argparse.ArgumentParser], None] | None = None

    def envelope(self, *, provider: str | None = None, variant: str | None = None, scenario_id: str | None = None) -> dict[str, Any]:
        """The fields the factory compares with its command, on every result this producer writes.

        ``variant`` is the authored variant of the cell, which is None for a
        cell the schema gives none: the execution key the factory passes as
        ``--variant`` is not it. ``provider`` defaults to the one provider a
        registration names.
        """

        registration = self.registration
        if provider is None and len(registration.providers) == 1:
            provider = registration.providers[0]
        envelope: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": self.artifact_kind,
            "producer": registration.to_dict(),
            "provider": provider,
            "variant": variant,
            "scenario_id": scenario_id or registration.scenario_id,
            "scenario_revision": registration.scenario_revision,
            "evidence_class": registration.evidence_classes[0],
            "generated_at": now(),
        }
        if registration.observation_scope == "scenario":
            envelope["observation_scope"] = "scenario"
        return envelope

    def verdict(self, run: Run, **identity: Any) -> dict[str, Any]:
        """The result of a run that reached its assertions."""

        return {
            **self.envelope(**identity),
            "status": "pass" if run.assertions and all(run.assertions.values()) else "fail",
            "observation": run.observation,
            "assertions": dict(run.assertions),
            **run.fields,
            "artifact_manifest": artifact_manifest(run.root),
        }

    def failure(self, run: Run, exc: BaseException, *, status: str = "fail", **identity: Any) -> dict[str, Any]:
        """The result of a run that raised: typed, with the verdict it had reached if that verdict fails."""

        code = getattr(exc, "failure_code", None) or (self.failure_code(exc) if callable(self.failure_code) else self.failure_code)
        failure = {
            **self.envelope(**identity),
            "status": status,
            "failure_code": code,
            "error": f"{type(exc).__name__}: {exc}",
            **failure_fields(exc),
            **failure_claim(exc),
            **run.fields,
            "artifact_manifest": artifact_manifest(run.root),
        }
        return settle_failed_result(failure, observation=run.observation, assertions=run.assertions)

    def execute(
        self,
        root: Path,
        body: Callable[[Run], object],
        *,
        creates_root: bool = True,
        raises: bool = False,
        failure_status: str = "fail",
        failure_cleanup: dict[str, Any] | None = None,
        **identity: Any,
    ) -> dict[str, Any]:
        """Run ``body`` in a fresh evidence root and write the result it comes to.

        The root must not exist: a producer never writes into another run's
        evidence. ``creates_root`` is False for a producer whose own work
        creates it. ``body`` raises to fail; whatever it had put on the run by
        then is what the failure keeps. ``failure_cleanup`` is the cleanup
        receipt a failed run leaves when it did not get as far as its own.
        ``raises`` is for a caller that types the failure itself (the release
        identity lane has its own outcome for an exception): nothing is
        written for a run that raised.
        """

        run = Run(root=root.resolve())
        if creates_root:
            run.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        try:
            body(run)
            result = self.verdict(run, **identity)
            run.write("result.json", result)
        except Exception as exc:  # noqa: BLE001 - a producer leaves one typed artifact, whatever raised
            if raises:
                raise
            if failure_cleanup is not None and not (run.root / "cleanup-receipt.json").exists():
                run.write("cleanup-receipt.json", {**failure_cleanup, "error_type": type(exc).__name__})
            result = self.failure(run, exc, status=failure_status, **identity)
            run.write("result.json", result)
        return result

    def parser(self) -> argparse.ArgumentParser:
        """The arguments the factory passes a producer, and this producer's own.

        Every producer parses the same envelope, including the authored cell
        axes it may not use, so a staged cell cannot silently run a different
        subject or evidence class and no producer invents a dialect that only
        fails in the live image.
        """

        parser = argparse.ArgumentParser()
        parser.add_argument("--variant", required=self.variant_required, choices=self.variants)
        for name in ("--evidence-root", "--repo-root", "--engine", "--longhouse-cli"):
            parser.add_argument(name, type=Path)
        parser.add_argument("--provider-bin", *self.provider_bin_aliases, dest="provider_bin", type=Path)
        if self.registration.providers:  # a product subject is not asked for a provider
            parser.add_argument("--provider", choices=self.registration.providers)
        for name in ("--provider-version", "--platform", "--architecture", "--evidence-class"):
            parser.add_argument(name)
        parser.add_argument("--credential-binding", action="append", default=[])
        parser.add_argument("--registration", action="store_true")
        parser.add_argument("--model", default=os.environ.get(self.model_env) if self.model_env else None)
        if self.negative_controls:
            parser.add_argument("--negative-control", choices=self.negative_controls)
        if self.runtime_host:
            parser.add_argument("--api-url")
            parser.add_argument("--agents-token")
        if self.add_arguments is not None:
            self.add_arguments(parser)
        return parser

    def main(self, invoke: Callable[[argparse.Namespace], dict[str, Any]], argv: Sequence[str] | None = None) -> int:
        """The producer's command line.

        ``--registration`` prints the registration and runs nothing. Otherwise
        the arguments are parsed and, in this order, a missing needed argument, a
        Runtime Host without its URL and token, a binary that is not an
        executable file and the producer's own precondition are each refused
        with a typed code and exit status 2, before anything is written.
        Then ``invoke`` runs, its result is printed, and the exit status is 0
        for a pass and 1 for anything else.
        """

        arguments = list(sys.argv[1:] if argv is None else argv)
        if "--registration" in arguments:
            print(json.dumps(self.registration.to_dict(), indent=2, sort_keys=True))
            return 0
        args = self.parser().parse_args(arguments)
        for name in self.needs:
            if getattr(args, name) is None:
                return refusal(f"missing_required_argument:--{name.replace('_', '-')}")
        if self.runtime_host:
            args.api_url = args.api_url or os.environ.get(RUNTIME_API_URL_ENV, "")
            require_disposable_runtime(args.api_url)
            args.agents_token = args.agents_token or os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
            if not args.api_url or not args.agents_token:
                return refusal("runtime_host_control_credentials_missing")
        for name, failure_code in self.binaries:
            path = getattr(args, name)
            if path is None or not path.is_file() or not os.access(path, os.X_OK):
                return refusal(failure_code)
        if self.refuse is not None and (failure_code := self.refuse(args)):
            return refusal(failure_code)
        result = invoke(args)
        print(json.dumps(result, sort_keys=True, default=str))
        return 0 if result.get("status") == "pass" else 1
