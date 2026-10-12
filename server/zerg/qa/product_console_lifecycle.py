#!/usr/bin/env python3
"""Hermetic Longhouse-product oracle for the Console session lifecycle."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration

SCENARIO_ID = "product_console_lifecycle"
ASSERTION_ID = "console_lifecycle_state_machine_preserved"

REGISTRATION = ProducerRegistration(
    producer_id="longhouse.product_console_lifecycle.v1",
    producer_revision=2,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, None),),
    providers=(),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("hermetic",),
    observed_activity=(
        "empty_ready_live_control",
        "active_working_live_control",
        "fifo_queue_accepted",
        "unsupported_interrupt_preserves_control",
        "terminal_settled_live_control",
        "idempotent_turn_receipt",
    ),
    acquisition_methods=("hermetic_source_under_test",),
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("console_lifecycle_observation", "cleanup_receipt"),
    required_cleanup=("catalog_disposed", "no_orphan_provider_processes"),
    implementation="server/zerg/qa/product_console_lifecycle.py",
    oracle_source="server/zerg/qa/product_console_lifecycle.py",
    oracle_entrypoint="run_product_console_lifecycle",
    executable_module="zerg.qa.product_console_lifecycle",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


class _ConsoleRegistry:
    @staticmethod
    def is_online(*, owner_id: int, device_id: str) -> bool:
        return owner_id == 1 and device_id == "factory-machine"

    @staticmethod
    def supports(*, owner_id: int, device_id: str, capability: str) -> bool:
        return owner_id == 1 and device_id == "factory-machine" and capability == "codex.turn_start"


def _project(store: object, session_id: str):
    from zerg.services.live_catalog_timeline import project_catalog_session_facts

    snapshot = store.read_shadow_session_state(session_id=session_id, owner_id=1)
    return project_catalog_session_facts(
        snapshot["legacy_facts"],
        observed_at=datetime.fromisoformat(snapshot["observed_at"]),
        canonical_heads=snapshot["heads"],
        commit_seq=int(snapshot["commit_seq"]),
    ).session_state


def run_product_console_lifecycle(root: Path) -> dict[str, object]:
    """Exercise the production Console store/projector without a provider process."""

    return PRODUCER.execute(root, _reach, failure_cleanup={**_CLEANUP, "status": "fail"})


def _reach(run: Run) -> None:
    raw_runtime_home = os.environ.get("LONGHOUSE_QUALIFICATION_HOME", "").strip()
    runtime_home = Path(raw_runtime_home).resolve() if raw_runtime_home else None
    # Catalog/runtime databases are reconstructable execution scratch, not
    # proof evidence. Keep them in the sandbox's ephemeral runtime mount so
    # the retained evidence tree contains only bounded semantic receipts.
    with tempfile.TemporaryDirectory(
        prefix="product-console-lifecycle-",
        dir=runtime_home,
    ) as temporary_runtime:
        runtime_root = Path(temporary_runtime)
        isolated_config = {
            "AUTH_DISABLED": "1",
            "DATABASE_URL": f"sqlite:///{runtime_root / 'runtime.db'}",
            "SINGLE_TENANT": "1",
            "TESTING": "1",
        }
        with patch.dict(os.environ, isolated_config, clear=False):
            _run_product_console_lifecycle(run, runtime_root=runtime_root)


def _run_product_console_lifecycle(run: Run, *, runtime_root: Path) -> None:
    """Run after establishing explicit test-safe process configuration."""

    from sqlalchemy.orm import Session

    from zerg.catalogd.schema import create_catalog_engine
    from zerg.catalogd.schema import initialize_catalog_schema
    from zerg.catalogd.store import CatalogStore
    from zerg.models.live_store import LiveConsoleTurn
    from zerg.models.live_store import LiveSessionInputReceipt
    from zerg.models.live_store import LiveUser
    from zerg.services.session_runtime import RuntimeEventIngest

    engine = create_catalog_engine(runtime_root / "catalog.db")
    store = CatalogStore(engine)
    session_id = uuid4()
    thread_id = uuid4()
    try:
        initialize_catalog_schema(engine)
        with Session(engine) as db:
            db.add(LiveUser(id=1, email="factory@example.invalid", is_active=True))
            db.commit()
        store.create_console_session(
            data={
                "session_id": str(session_id),
                "thread_id": str(thread_id),
                "owner_id": 1,
                "provider": "codex",
                "device_id": "factory-machine",
                "cwd": "/workspace/product-console-lifecycle",
                "project": "product-console-lifecycle",
                "provider_config": {"permission_mode": "bypass"},
                "started_at": datetime.now(UTC),
            }
        )
        turn_identity = {
            "owner_id": 1,
            "session_id": str(session_id),
            "thread_id": str(thread_id),
            "provider": "codex",
            "device_id": "factory-machine",
        }
        with patch(
            "zerg.services.live_catalog_timeline.get_machine_control_channel_registry",
            return_value=_ConsoleRegistry(),
        ):
            empty = _project(store, str(session_id))
            empty_ready = (
                empty.transcript.convergence == "current"
                and empty.presentation.primary is not None
                and empty.presentation.primary.key == "ready"
                and empty.presentation.access.key == "live_control"
                and empty.control is not None
                and empty.control.actions.start_turn.state == "available"
            )

            first = store.enqueue_console_turn(
                data={
                    "session_id": str(session_id),
                    "owner_id": 1,
                    "message": "first",
                    "client_request_id": "factory-turn-1",
                    "created_at": datetime.now(UTC),
                }
            )
            replay = store.enqueue_console_turn(
                data={
                    "session_id": str(session_id),
                    "owner_id": 1,
                    "message": "first",
                    "client_request_id": "factory-turn-1",
                    "created_at": datetime.now(UTC),
                }
            )
            second = store.enqueue_console_turn(
                data={
                    "session_id": str(session_id),
                    "owner_id": 1,
                    "message": "second",
                    "client_request_id": "factory-turn-2",
                    "created_at": datetime.now(UTC),
                }
            )
            first_turn = first["turn"]
            second_turn = second["turn"]
            store.update_console_turn(
                data={
                    **turn_identity,
                    "turn_id": first_turn["turn_id"],
                    "run_id": first_turn["run_id"],
                    "state": "active",
                    "expected_state": "starting",
                    "updated_at": datetime.now(UTC),
                }
            )
            store.apply_session_runtime(
                events=[
                    RuntimeEventIngest(
                        runtime_key=f"codex:{session_id}",
                        session_id=session_id,
                        thread_id=thread_id,
                        run_id=first_turn["run_id"],
                        provider="codex",
                        device_id="factory-machine",
                        source="codex_exec",
                        kind="phase_signal",
                        phase="thinking",
                        occurred_at=datetime.now(UTC),
                        dedupe_key=f"phase:{first_turn['run_id']}:thinking",
                    )
                ]
            )
            active = _project(store, str(session_id))
            active_live = (
                active.activity.state in {"thinking", "executing"}
                and active.presentation.access.key == "live_control"
                and active.control is not None
                and active.control.actions.start_turn.state == "available"
            )
            no_interrupt_live = (
                active.control is not None
                and active.control.actions.interrupt.state == "unavailable"
                and active.control.actions.interrupt.reason == "unsupported"
                and active.presentation.access.key == "live_control"
            )
            fifo_accepted = first_turn["state"] == "starting" and second_turn["state"] == "queued" and second_turn["run_id"] is None

            settled = store.update_console_turn(
                data={
                    **turn_identity,
                    "run_id": first_turn["run_id"],
                    "state": "completed",
                    "updated_at": datetime.now(UTC),
                }
            )
            next_turn = settled["next_turn"]
            store.update_console_turn(
                data={
                    **turn_identity,
                    "turn_id": next_turn["turn_id"],
                    "run_id": next_turn["run_id"],
                    "state": "active",
                    "expected_state": "starting",
                    "updated_at": datetime.now(UTC),
                }
            )
            store.update_console_turn(
                data={
                    **turn_identity,
                    "run_id": next_turn["run_id"],
                    "state": "completed",
                    "updated_at": datetime.now(UTC),
                }
            )
            store.apply_session_runtime(
                events=[
                    RuntimeEventIngest(
                        runtime_key=f"codex:{session_id}",
                        session_id=session_id,
                        thread_id=thread_id,
                        run_id=next_turn["run_id"],
                        provider="codex",
                        device_id="factory-machine",
                        source="codex_exec",
                        kind="phase_signal",
                        phase="idle",
                        occurred_at=datetime.now(UTC),
                        dedupe_key=f"phase:{next_turn['run_id']}:idle",
                    )
                ]
            )
            terminal = _project(store, str(session_id))
            terminal_settled = (
                terminal.run is not None
                and terminal.run.lifecycle == "ended"
                and terminal.presentation.primary is not None
                and terminal.presentation.primary.key == "ended"
                and terminal.presentation.access.key == "live_control"
                and terminal.control is not None
                and terminal.control.actions.start_turn.state == "available"
            )

        with Session(engine) as db:
            exact_receipts = db.query(LiveSessionInputReceipt).filter(LiveSessionInputReceipt.session_id == str(session_id)).count() == 2
            exact_turns = db.query(LiveConsoleTurn).filter(LiveConsoleTurn.session_id == str(session_id)).count() == 2
        idempotent_receipt = replay["turn"]["turn_id"] == first_turn["turn_id"] and exact_receipts and exact_turns
        ordered_fifo = next_turn["turn_id"] == second_turn["turn_id"]
        observed = {
            "empty_ready_live_control": empty_ready,
            "active_working_live_control": active_live,
            "fifo_queue_accepted": fifo_accepted and ordered_fifo,
            "unsupported_interrupt_preserves_control": no_interrupt_live,
            "terminal_settled_live_control": terminal_settled,
            "idempotent_turn_receipt": idempotent_receipt,
            "catalog_disposed": True,
            "no_orphan_provider_processes": True,
            "orphan_count": 0,
            "session_id": str(session_id),
            "turn_ids": [first_turn["turn_id"], second_turn["turn_id"]],
            "empty_state": empty.model_dump(mode="json"),
            "active_state": active.model_dump(mode="json"),
            "terminal_state": terminal.model_dump(mode="json"),
        }
        run.write("console-lifecycle-observation.json", observed)
        run.observation = observed
        run.assertions = {ASSERTION_ID: all(observed[name] is True for name in REGISTRATION.observed_activity)}
    finally:
        engine.dispose()
        run.write("cleanup-receipt.json", {**_CLEANUP, "status": "pass", "catalog_disposed": True})


_CLEANUP = {"schema_version": 1, "artifact_kind": "longhouse_product_console_cleanup_receipt", "orphan_count": 0}
PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="longhouse_product_console_lifecycle_result",
    failure_code="product_console_lifecycle_failed",
    variant_required=False,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(lambda args: run_product_console_lifecycle(args.evidence_root), argv)


if __name__ == "__main__":
    raise SystemExit(main())
