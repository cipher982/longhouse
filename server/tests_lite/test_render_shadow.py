"""Host render shadow: stored raw plus the capture witness goes to the normalizer.

The engine's own tests prove its rebuild equals the laptop render for every
fixture. These prove the host side: the sweep hands the normalizer exactly the
stored bytes and witness of each envelope, compares its answer with the stored
laptop render, records one outcome per envelope, and survives a normalizer
that errors or dies. The normalizer here is a fake speaking the worker
protocol; the real engine runs in the image and on canary.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests_lite.test_agents_storage_v2 import _InlineRenderPool
from tests_lite.test_agents_storage_v2 import _payload
from tests_lite.test_agents_storage_v2 import _storage_v2_stack
from zerg.config import get_settings
from zerg.services.render_shadow import NormalizerWorker
from zerg.services.render_shadow import shadow_session
from zerg.storage_v2.render_objects import read_render_object

FAKE_NORMALIZER = r"""
import base64, json, os, sys

assert sys.argv[1:] == ["normalize-worker"], sys.argv
mode = os.environ["FAKE_NORMALIZER_MODE"]
render = json.loads(os.environ["FAKE_NORMALIZER_RENDER"])
for line in sys.stdin:
    request = json.loads(line)
    if "envelopes" not in request:
        print(json.dumps({"id": None, "error": "bad request: missing field", "revision": os.environ.get("FAKE_NORMALIZER_REVISION", "engine-parser-v2+cursor-fake")}), flush=True)
        continue
    if mode == "die":
        sys.exit(3)
    answers = []
    for envelope in request["envelopes"]:
        data = b"".join(base64.b64decode(record["data_b64"]) for record in envelope["records"])
        witnessed = [fact for fact in envelope["facts"] if fact["kind"] == "capture.witness"]
        if data != b"hello\n" or len(witnessed) != 1 or witnessed[0]["payload"] != {"format": "codex"}:
            answers.append({"range_start": envelope["range_start"], "range_end": envelope["range_end"], "error": f"bad input {data!r} {witnessed!r}"})
            continue
        if mode == "short":
            continue
        records = [dict(record) for record in render]
        if mode == "drift":
            records[1]["content_text"] = "WORLD"
        answers.append({
            "range_start": envelope["range_start"],
            "range_end": envelope["range_end"],
            "render": {"records": records, "session": {}, "media": [], "facts": []},
        })
    generation = json.loads(os.environ.get("FAKE_NORMALIZER_GENERATION", "null"))
    print(json.dumps({"id": request["id"], "envelopes": answers, "generation": generation, "elapsed_us": 1, "revision": os.environ.get("FAKE_NORMALIZER_REVISION", "engine-parser-v2+cursor-fake")}), flush=True)
"""


class _ShadowRenderPool(_InlineRenderPool):
    async def read(self, object_path, expected_object_hash, *, lane, queue_timeout_seconds=None, operation_timeout_seconds=None):
        assert lane in {"background", "user"}
        return read_render_object(self.root, object_path, expected_object_hash=expected_object_hash)


def _fake_normalizer(tmp_path: Path, monkeypatch, *, mode: str, render: list[dict]) -> str:
    script = tmp_path / "fake-engine"
    script.write_text(f"#!{sys.executable}\n{FAKE_NORMALIZER}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_NORMALIZER_MODE", mode)
    monkeypatch.setenv("FAKE_NORMALIZER_RENDER", json.dumps(render))
    return str(script)


async def _commit(stack, *, witnessed: bool, laptop_render: bool = True) -> tuple[str, dict]:
    payload = _payload(tenant_id=get_settings().archive_primary_tenant_id, machine_id="cinder", epoch=uuid4())
    session_id = str(uuid4())
    payload["session_id"] = session_id
    if not laptop_render:
        payload["render"] = None
    if witnessed:
        payload["facts"] = [
            {"kind": "capture.witness", "at": "2026-07-12T12:00:00+00:00", "source_position": 0, "payload": {"format": "codex"}}
        ]
    response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
    assert response.status_code == 200, response.text
    return session_id, payload


async def _shadow(stack, worker, session_id):
    return await shadow_session(
        catalog=stack.catalog,
        raw_workers=stack.raw_workers,
        render_workers=stack.render_workers,
        worker=worker,
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_a_host_rebuild_equal_to_the_laptop_render_is_recorded_as_a_match(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        candidates = await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": None})
        assert [row["session_id"] for row in candidates["sessions"]] == [session_id]

        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="match", render=payload["render"]["records"]))
        try:
            results = await _shadow(stack, worker, session_id)
        finally:
            await worker.close()
        assert [(row["outcome"], row["detail"], row["normalizer_revision"]) for row in results] == [
            ("match", None, "engine-parser-v2+cursor-fake")
        ]

        await stack.catalog.call("render.shadow.record.v2", {"results": results})
        assert (await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": None}))["sessions"] == []
        summary = await stack.catalog.call("render.shadow.summary.v2", {})
        assert summary["counts"] == [{"provider": "codex", "outcome": "match", "envelopes": 1, "sessions": 1}]


@pytest.mark.asyncio
async def test_a_rebuild_that_differs_names_the_first_differing_field(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-drift-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="drift", render=payload["render"]["records"]))
        try:
            results = await _shadow(stack, worker, session_id)
        finally:
            await worker.close()
        assert [row["outcome"] for row in results] == ["mismatch"]
        assert "assistant-1" in results[0]["detail"] and "content_text" in results[0]["detail"]


@pytest.mark.asyncio
async def test_an_envelope_without_a_witness_is_classified_without_calling_the_normalizer(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-old-") as stack:
        session_id, _payload_unused = await _commit(stack, witnessed=False)
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="die", render=[]))
        try:
            results = await _shadow(stack, worker, session_id)
            assert worker._process is None
        finally:
            await worker.close()
        assert [row["outcome"] for row in results] == ["no_witness"]


@pytest.mark.asyncio
async def test_a_dead_normalizer_is_recorded_as_an_error_and_restarted(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-die-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        binary = _fake_normalizer(tmp_path, monkeypatch, mode="die", render=payload["render"]["records"])
        worker = NormalizerWorker(binary)
        try:
            results = await _shadow(stack, worker, session_id)
            assert [row["outcome"] for row in results] == ["error"]
            assert "normalizer" in results[0]["detail"]

            monkeypatch.setenv("FAKE_NORMALIZER_MODE", "match")
            results = await _shadow(stack, worker, session_id)
            assert [row["outcome"] for row in results] == ["match"]
        finally:
            await worker.close()
        assert worker._process is None


def test_the_normalizer_is_only_the_engine_built_into_the_image(monkeypatch, tmp_path):
    from zerg.services import render_shadow

    monkeypatch.delenv("LONGHOUSE_NORMALIZER_BIN", raising=False)
    monkeypatch.setattr(render_shadow, "DEFAULT_BINARY", str(tmp_path / "absent"))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    (tmp_path / "longhouse-engine").write_text("#!/bin/sh\n")
    (tmp_path / "longhouse-engine").chmod(0o755)
    assert render_shadow.normalizer_binary() is None


@pytest.mark.asyncio
async def test_a_session_compared_recently_waits_before_its_new_envelopes_are_compared(monkeypatch, tmp_path):
    from tests_lite.test_storage_v2_provider_facts import _payload_for_source
    from zerg.catalogd.store_mixins import render_shadow as shadow_store

    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-cool-") as stack:
        session_id, payload = await _commit(stack, witnessed=False)
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="die", render=[]))
        try:
            await stack.catalog.call("render.shadow.record.v2", {"results": await _shadow(stack, worker, session_id)})
        finally:
            await worker.close()

        later = _payload_for_source(tenant_id=get_settings().archive_primary_tenant_id, epoch=uuid4(), opaque_source_id="later.jsonl")
        later["session_id"] = session_id
        later["render"]["generation_id"] = payload["render"]["generation_id"]
        response = await stack.client.post("/agents/storage/v2/envelopes", json=later, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text
        assert (await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": None}))["sessions"] == []

        monkeypatch.setattr(shadow_store, "SHADOW_SESSION_COOLDOWN", shadow_store.timedelta(0))
        candidates = await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": None})
        assert [row["session_id"] for row in candidates["sessions"]] == [session_id]


@pytest.mark.asyncio
async def test_a_failed_read_or_a_missing_answer_is_an_error_not_a_mismatch(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-err-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="short", render=payload["render"]["records"]))
        try:
            results = await _shadow(stack, worker, session_id)
            assert [(row["outcome"], row["detail"]) for row in results] == [("error", "normalizer answered 0 envelopes for 1")]

            async def busy(*_args, **_kwargs):
                raise RuntimeError("raw repair queue is full")

            monkeypatch.setattr(stack.raw_workers, "read", busy)
            results = await _shadow(stack, worker, session_id)
            assert [row["outcome"] for row in results] == ["error"]
            assert "raw repair queue is full" in results[0]["detail"]
        finally:
            await worker.close()


@pytest.mark.asyncio
async def test_a_new_normalizer_revision_reopens_matches_and_errors_are_retried(monkeypatch, tmp_path):
    from zerg.catalogd.store_mixins import render_shadow as shadow_store

    monkeypatch.setattr(shadow_store, "SHADOW_SESSION_COOLDOWN", shadow_store.timedelta(0))
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-rev-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="match", render=payload["render"]["records"]))
        try:
            results = await _shadow(stack, worker, session_id)
        finally:
            await worker.close()
        assert worker.revision == "engine-parser-v2+cursor-fake"
        await stack.catalog.call("render.shadow.record.v2", {"results": results})

        async def candidates(revision):
            listed = await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": revision})
            return [row["session_id"] for row in listed["sessions"]]

        assert await candidates("engine-parser-v2+cursor-fake") == []
        assert await candidates(None) == []
        assert await candidates("next-rev") == [session_id]

        await stack.catalog.call("render.shadow.record.v2", {"results": [{**results[0], "outcome": "error"}]})
        assert await candidates("engine-parser-v2+cursor-fake") == [session_id]


@pytest.mark.asyncio
async def test_a_session_too_large_to_rebuild_is_recorded_and_leaves_the_queue(monkeypatch, tmp_path):
    from zerg.catalogd.store_mixins import render_shadow as shadow_store

    monkeypatch.setattr(shadow_store, "MAX_SHADOW_RAW_OBJECTS", 0)
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-big-") as stack:
        session_id, _unused = await _commit(stack, witnessed=True)
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="die", render=[]))
        try:
            results = await _shadow(stack, worker, session_id)
        finally:
            await worker.close()
        assert [row["outcome"] for row in results] == ["too_large"]
        # An earlier error must not keep an oversized session queued forever.
        await stack.catalog.call("render.shadow.record.v2", {"results": [dict(results[0], outcome="error", detail="earlier")]})
        monkeypatch.setattr(shadow_store, "SHADOW_SESSION_COOLDOWN", shadow_store.timedelta(0))
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="die", render=[]))
        try:
            results = await _shadow(stack, worker, session_id)
        finally:
            await worker.close()
        assert [row["outcome"] for row in results] == ["too_large"]
        await stack.catalog.call("render.shadow.record.v2", {"results": results})
        listed = await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": None})
        assert listed["sessions"] == []


@pytest.mark.asyncio
async def test_the_worker_learns_its_revision_from_a_probe(monkeypatch, tmp_path):
    worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="match", render=[]))
    try:
        assert await worker.probe() == "engine-parser-v2+cursor-fake"
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_a_session_whose_catalog_call_fails_waits_and_the_batch_goes_on(monkeypatch, tmp_path):
    from zerg.services import render_shadow

    calls: list[tuple[str, dict]] = []

    class _Catalog:
        async def call(self, method, params):
            calls.append((method, params))
            if method == "render.shadow.candidates.v2":
                if sum(1 for name, _ in calls if name == method) > 2:
                    raise asyncio.CancelledError
                return {"sessions": [{"session_id": "bad"}, {"session_id": "good"}]}
            if method == "storage.session.rebuild_inputs.v2":
                if params["session_id"] == "bad":
                    raise RuntimeError("catalog timeout")
                return {"found": False}
            raise AssertionError(method)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(render_shadow.asyncio, "sleep", no_sleep)
    worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="match", render=[]))
    try:
        with pytest.raises(asyncio.CancelledError):
            await render_shadow._sweep(_Catalog(), None, None, worker)
    finally:
        await worker.close()
    assert worker.revision == "engine-parser-v2+cursor-fake"
    inputs = [params["session_id"] for method, params in calls if method == "storage.session.rebuild_inputs.v2"]
    # The failing session is skipped on the next tick; the good one is not.
    assert inputs == ["bad", "good", "good"]
    listed = [params for method, params in calls if method == "render.shadow.candidates.v2"]
    assert listed[1]["limit"] == render_shadow.SWEEP_BATCH + 1


@pytest.mark.asyncio
async def test_deleting_a_session_drops_its_shadow_rows_and_late_results_are_ignored(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-del-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="drift", render=payload["render"]["records"]))
        try:
            results = await _shadow(stack, worker, session_id)
        finally:
            await worker.close()
        await stack.catalog.call("render.shadow.record.v2", {"results": results})
        assert (await stack.catalog.call("render.shadow.summary.v2", {}))["samples"]

        await stack.catalog.call(
            "storage.session.delete.v2",
            {"session_id": session_id, "deletion_id": str(uuid4()), "reason": "test", "deleted_at": "2026-11-14T12:00:00+00:00"},
        )
        assert (await stack.catalog.call("render.shadow.summary.v2", {}))["samples"] == []

        late = await stack.catalog.call("render.shadow.record.v2", {"results": results})
        assert late["recorded"] == 0
        assert (await stack.catalog.call("render.shadow.summary.v2", {}))["counts"] == []


def test_stored_times_reach_the_engine_as_rfc3339_utc():
    from datetime import UTC
    from datetime import datetime

    from zerg.catalogd.store_mixins.render_shadow import _rfc3339

    assert _rfc3339("2026-11-14 17:56:42.158704") == "2026-11-14T17:56:42.158704+00:00"
    assert _rfc3339("2026-11-14T12:56:42-05:00") == "2026-11-14T17:56:42+00:00"
    assert _rfc3339(datetime(2026, 11, 14, 17, 56, 42, tzinfo=UTC)) == "2026-11-14T17:56:42+00:00"


@pytest.mark.asyncio
async def test_a_laptop_render_from_another_parser_revision_is_skew_not_a_mismatch(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-shadow-skew-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        monkeypatch.setenv("FAKE_NORMALIZER_REVISION", "engine-parser-v3+cursor-fake")
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="drift", render=payload["render"]["records"]))
        try:
            results = await _shadow(stack, worker, session_id)
        finally:
            await worker.close()
        assert [(row["outcome"], row["detail"]) for row in results] == [
            ("revision_skew", "laptop engine-parser-v2 host engine-parser-v3+cursor-fake")
        ]


def test_a_laptop_render_is_comparable_when_its_revision_is_either_host_parser():
    from zerg.services.render_shadow import comparable_revisions

    assert comparable_revisions("engine-parser-v2+cursor-store-render-v8") == {"engine-parser-v2", "cursor-store-render-v8"}
    assert comparable_revisions(None) == frozenset()


@pytest.mark.asyncio
async def test_a_host_revision_bump_serves_the_host_render_from_a_host_generation(monkeypatch, tmp_path):
    """Phase 4 check: with an older engine's render stored, a host revision
    bump moves the session to a host generation built from raw, with no
    laptop release."""
    from zerg.services.render_shadow import _take_over

    monkeypatch.setenv("LONGHOUSE_HOST_RENDER", "1")
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-host-bump-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        host_generation = str(uuid4())
        monkeypatch.setenv("FAKE_NORMALIZER_REVISION", "engine-parser-v3+cursor-fake")
        monkeypatch.setenv(
            "FAKE_NORMALIZER_GENERATION",
            json.dumps({"generation_id": host_generation, "parser_revision": "engine-parser-v3", "ordering_revision": "semantic-order-v2"}),
        )
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="drift", render=payload["render"]["records"]))
        try:
            results = await _shadow(stack, worker, session_id)
            assert [row["outcome"] for row in results] == ["revision_skew"]
            results = await _take_over(
                catalog=stack.catalog,
                raw_workers=stack.raw_workers,
                render_workers=stack.render_workers,
                worker=worker,
                session_id=session_id,
                results=results,
            )
        finally:
            await worker.close()
        assert [row["outcome"] for row in results] == ["host_rendered"]
        session = (await stack.catalog.call("storage.session.read.v2", {"session_id": session_id}))["session"]
        assert session["current_render_generation"] == host_generation
        page = await stack.client.get(f"/agents/storage/v2/sessions/{session_id}/events")
        assert page.status_code == 200, page.text
        assert [event["content_text"] for event in page.json()["events"]] == ["hello", "WORLD"]


@pytest.mark.asyncio
async def test_a_host_generation_is_not_served_until_it_covers_the_current_one(monkeypatch, tmp_path):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-host-cover-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        envelope_id = payload["expected_envelope_id"]
        current = (await stack.catalog.call("storage.session.read.v2", {"session_id": session_id}))["session"]["current_render_generation"]
        answer = await stack.catalog.call(
            "storage.render.host_commit.v2",
            {
                "session_id": session_id,
                "generation_id": str(uuid4()),
                "parser_revision": "engine-parser-v9",
                "ordering_revision": "semantic-order-v2",
                "objects": [],
                "promote": True,
            },
        )
        assert answer["current"] is False
        assert (await stack.catalog.call("storage.session.read.v2", {"session_id": session_id}))["session"][
            "current_render_generation"
        ] == current
        assert envelope_id


@pytest.mark.asyncio
async def test_ingest_seals_the_host_render_of_a_witnessed_envelope(monkeypatch, tmp_path):
    from zerg.services.host_render import close_ingest_normalizer

    monkeypatch.setenv("LONGHOUSE_HOST_RENDER", "1")
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-host-ingest-") as stack:
        laptop_records = _payload(tenant_id=get_settings().archive_primary_tenant_id, machine_id="cinder", epoch=uuid4())["render"][
            "records"
        ]
        monkeypatch.setenv("LONGHOUSE_NORMALIZER_BIN", _fake_normalizer(tmp_path, monkeypatch, mode="drift", render=laptop_records))
        monkeypatch.setenv(
            "FAKE_NORMALIZER_GENERATION",
            json.dumps(
                {
                    "generation_id": "018f0c3a-7b2d-7f10-8a11-423456789abc",
                    "parser_revision": "engine-parser-v2",
                    "ordering_revision": "semantic-order-v2",
                }
            ),
        )
        try:
            session_id, _payload_sent = await _commit(stack, witnessed=True)
        finally:
            await close_ingest_normalizer()
        page = await stack.client.get(f"/agents/storage/v2/sessions/{session_id}/events")
        assert page.status_code == 200, page.text
        # The laptop shipped "world"; the host rendered "WORLD" from raw and that is served.
        assert [event["content_text"] for event in page.json()["events"]] == ["hello", "WORLD"]
        inputs = await stack.catalog.call("storage.session.rebuild_inputs.v2", {"session_id": session_id})
        assert inputs["current_render_generation"] == "018f0c3a-7b2d-7f10-8a11-423456789abc"


@pytest.mark.asyncio
async def test_a_witnessed_envelope_without_a_laptop_render_is_served_from_the_host_render(monkeypatch, tmp_path):
    from zerg.services.host_render import close_ingest_normalizer

    monkeypatch.setenv("LONGHOUSE_HOST_RENDER", "1")
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-host-norender-") as stack:
        records = _payload(tenant_id=get_settings().archive_primary_tenant_id, machine_id="cinder", epoch=uuid4())["render"]["records"]
        monkeypatch.setenv("LONGHOUSE_NORMALIZER_BIN", _fake_normalizer(tmp_path, monkeypatch, mode="match", render=records))
        monkeypatch.setenv(
            "FAKE_NORMALIZER_GENERATION",
            json.dumps({"generation_id": str(uuid4()), "parser_revision": "engine-parser-v2", "ordering_revision": "semantic-order-v2"}),
        )
        capabilities = await stack.client.get("/agents/storage/v2/capabilities", headers={"X-Longhouse-Machine-Id": "cinder"})
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["host_render"] is True
        try:
            session_id, _sent = await _commit(stack, witnessed=True, laptop_render=False)
        finally:
            await close_ingest_normalizer()
        page = await stack.client.get(f"/agents/storage/v2/sessions/{session_id}/events")
        assert page.status_code == 200, page.text
        assert [event["content_text"] for event in page.json()["events"]] == ["hello", "world"]


@pytest.mark.asyncio
async def test_a_dry_rebuild_compares_host_event_counts_with_the_served_render(monkeypatch, tmp_path):
    from zerg.services.host_render import host_rebuild_session

    async with _storage_v2_stack(monkeypatch, render_pool_factory=_ShadowRenderPool, prefix="lh2-host-dry-") as stack:
        session_id, payload = await _commit(stack, witnessed=True)
        monkeypatch.setenv(
            "FAKE_NORMALIZER_GENERATION",
            json.dumps({"generation_id": str(uuid4()), "parser_revision": "engine-parser-v2", "ordering_revision": "semantic-order-v2"}),
        )
        worker = NormalizerWorker(_fake_normalizer(tmp_path, monkeypatch, mode="match", render=payload["render"]["records"]))
        try:
            result = await host_rebuild_session(
                catalog=stack.catalog,
                raw_workers=stack.raw_workers,
                render_workers=stack.render_workers,
                worker=worker,
                session_id=session_id,
                commit=False,
            )
        finally:
            await worker.close()
        assert (result["compared"], result["equal"], result["differences"]) == (1, 1, [])
        # Nothing was committed: the laptop generation still serves.
        session = (await stack.catalog.call("storage.session.read.v2", {"session_id": session_id}))["session"]
        assert session["current_render_generation"] == payload["render"]["generation_id"]
