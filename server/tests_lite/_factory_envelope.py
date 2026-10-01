"""Hold a producer's result to the envelope the provider factory compares it with.

``zerg.qa.factory_registration.result_envelope_failures`` restates the factory's check; this
builds the command the factory would have run for an execution variant (the ``--variant`` it
passes) and asserts the result answers it. Pass the variant the test invoked the producer with.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any
from typing import Mapping

from zerg.qa import factory_registration

SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "managed_providers.yml"


def assert_result_conforms(producer: ModuleType, result: Mapping[str, Any], *, variant: str | None, provider: str | None = None) -> None:
    registration = producer.REGISTRATION.to_dict()
    commands = [
        command
        for command in factory_registration.factory_commands(registration, factory_registration.contract_rows(SCHEMA))
        if command["execution_variant"] == variant and provider in (None, command["provider"])
    ]
    assert commands, f"{registration['producer_id']} covers no row run with --variant {variant!r}"
    problems = {
        f"{command['provider']}:{command['assertion_id']}": failures
        for command in commands
        if (failures := factory_registration.result_envelope_failures(command, result))
    }
    assert not problems, f"the factory would refuse {registration['producer_id']}'s result: {problems}"
