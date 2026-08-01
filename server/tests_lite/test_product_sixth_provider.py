from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.services.managed_provider_contracts as contract_registry  # noqa: E402
from zerg.managed_provider_contract_manifest import normalize_contract_manifest  # noqa: E402
from zerg.qa import provider_adapters  # noqa: E402
from zerg.qa import universal_agent_harness as harness  # noqa: E402
from zerg.services.machine_control_channel import get_machine_control_channel_registry  # noqa: E402
from zerg.services.machines_directory import build_machines_directory  # noqa: E402
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT  # noqa: E402
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL  # noqa: E402
from zerg.services.managed_control_dispatcher import dispatch_managed_control_command  # noqa: E402
from zerg.services.managed_provider_contracts import managed_provider_contract_from_item  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PROVIDER = "toy_sixth"


def _load_brand_generator():
    path = REPO / "scripts" / "generate" / "provider_brands.py"
    spec = importlib.util.spec_from_file_location("product_sixth_provider_brand_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _toy_schema_entry(adapter_source: str) -> dict[str, object]:
    manifest = json.loads((REPO / "server/zerg/config/managed_provider_contracts.json").read_text())
    item = deepcopy(next(entry for entry in manifest["providers"] if entry["provider"] == "cursor"))
    item.update(
        {
            "provider": PROVIDER,
            "display_name": "Toy Sixth",
            "marketing_name": "Toy Sixth Agent",
            "provider_cli_binary": "toy-sixth",
            "provider_cli_env": "LONGHOUSE_TOY_SIXTH_BIN",
            "control_plane": "toy_sixth_bridge",
            "control_plane_aliases": [],
            "adapter_sources": [adapter_source],
            "capabilities": {},
        }
    )
    item.pop("adapter_digest", None)
    item["machine_control_supports"] = [
        str(support).replace("cursor.", f"{PROVIDER}.") for support in item["machine_control_supports"]
    ]
    return item


class _CompletingMachineWebSocket:
    def __init__(self, registry, *, owner_id: int, device_id: str):
        self.registry = registry
        self.owner_id = owner_id
        self.device_id = device_id
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)
        await self.registry.complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {"exit_code": 0, "stdout": "accepted", "stderr": ""},
            },
            owner_id=self.owner_id,
            device_id=self.device_id,
        )


def test_schema_and_adapter_directory_onboard_a_sixth_product_provider(tmp_path, monkeypatch):
    """One contract entry and adapter module reach every generic product seam."""

    adapter_dir = tmp_path / "server/zerg/qa/provider_adapters"
    adapter_dir.mkdir(parents=True)
    adapter_path = adapter_dir / f"{PROVIDER}.py"
    adapter_path.write_text(
        "from zerg.qa.universal_agent_harness import UniversalProviderAdapter\n"
        "from zerg.qa.universal_agent_harness import register_adapter\n\n"
        f'@register_adapter("{PROVIDER}")\n'
        "class ToySixthAdapter(UniversalProviderAdapter):\n"
        '    """Synthetic provider used only by the product onboarding gate."""\n',
        encoding="utf-8",
    )
    relative_adapter = str(adapter_path.relative_to(tmp_path))
    normalized = normalize_contract_manifest(
        {"schema_version": 1, "providers": [_toy_schema_entry(relative_adapter)]},
        source_root=tmp_path,
    )
    toy_item = normalized["providers"][0]
    toy_contract = managed_provider_contract_from_item(toy_item)
    assert toy_contract.display_name == "Toy Sixth"
    assert toy_contract.machine_control_operations == ("send", "interrupt", "terminate", "turn_start", "turn_interrupt")

    # A schema-only provider receives default visuals and canonical labels in
    # every generated client; provider-brands.json needs no provider entry.
    brand_generator = _load_brand_generator()
    brands = json.loads((REPO / "config/provider-brands.json").read_text())
    current_manifest = json.loads((REPO / "server/zerg/config/managed_provider_contracts.json").read_text())
    merged = brand_generator.merge_managed_provider_identity(
        brands,
        {**current_manifest, "providers": [*current_manifest["providers"], toy_item]},
    )
    assert PROVIDER not in brands["providers"]
    assert 'displayName: "Toy Sixth"' in brand_generator.render_ts(merged)
    assert 'displayName: "Toy Sixth"' in brand_generator.render_swift(merged)
    assert '"toy_sixth": "Toy Sixth"' in brand_generator.render_python(merged)

    # The adapter package discovers the new module without a provider import or
    # registry edit. This is the same production loader the factory uses.
    module_name = f"{provider_adapters.__name__}.{PROVIDER}"
    monkeypatch.setattr(provider_adapters, "__path__", [str(adapter_dir)])
    assert PROVIDER not in harness.ADAPTER_CLASS_BY_PROVIDER
    provider_adapters.load_all()
    assert harness.ADAPTER_CLASS_BY_PROVIDER[PROVIDER].__name__ == "ToySixthAdapter"

    existing_contracts = contract_registry._CONTRACTS
    monkeypatch.setattr(contract_registry, "_CONTRACTS", (*existing_contracts, toy_contract))
    monkeypatch.setattr(contract_registry, "_BY_PROVIDER", {**contract_registry._BY_PROVIDER, PROVIDER: toy_contract})

    async def _exercise_product_surface():
        registry = get_machine_control_channel_registry()
        await registry.clear_for_tests()
        websocket = _CompletingMachineWebSocket(registry, owner_id=42, device_id="toy-machine")
        try:
            await registry.register(
                owner_id=42,
                device_id="toy-machine",
                machine_name="toy-machine",
                engine_build="test-build",
                supports=[f"{PROVIDER}.send", f"{PROVIDER}.turn_start"],
                websocket=websocket,
            )
            machine = build_machines_directory(owner_id=42, enrollments=[], registry=registry)[0]
            assert machine.control_operations_by_provider == {PROVIDER: ("send", "turn_start")}
            assert [option.provider for option in machine.launch.providers] == [PROVIDER]
            assert machine.launch.default_provider == PROVIDER

            session = SimpleNamespace(
                id=uuid4(),
                device_id="toy-machine",
                provider=PROVIDER,
                execution_home="managed_local",
                managed_transport=toy_contract.managed_transport.value,
                source_runner_id=None,
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "hello sixth provider"},
                request_id="sixth-provider-gate",
            )
            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert websocket.sent[0]["payload"] == {
                "provider": PROVIDER,
                "text": "hello sixth provider",
            }
        finally:
            await registry.clear_for_tests()

    try:
        asyncio.run(_exercise_product_surface())
    finally:
        harness.ADAPTER_CLASS_BY_PROVIDER.pop(PROVIDER, None)
        sys.modules.pop(module_name, None)
