"""Host render shadow: stored raw plus the capture witness goes to the normalizer.

The engine's own tests prove its rebuild equals the laptop render for every
fixture. These prove the host side: the sweep hands the normalizer exactly the
stored bytes and witness of each envelope, compares its answer with the stored
laptop render, records one outcome per envelope, and survives a normalizer
that errors or dies. The normalizer here is a fake speaking the worker
protocol; the real engine runs in the image and on canary.
"""

from __future__ import annotations

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
    print(json.dumps({"id": request["id"], "envelopes": answers, "elapsed_us": 1, "revision": "fake-rev"}), flush=True)
"""


class _ShadowRenderPool(_InlineRenderPool):
    async def read(self, object_path, expected_object_hash, *, lane, queue_timeout_seconds=None, operation_timeout_seconds=None):
        assert lane == "background"
        return read_render_object(self.root, object_path, expected_object_hash=expected_object_hash)


def _fake_normalizer(tmp_path: Path, monkeypatch, *, mode: str, render: list[dict]) -> str:
    script = tmp_path / "fake-engine"
    script.write_text(f"#!{sys.executable}\n{FAKE_NORMALIZER}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_NORMALIZER_MODE", mode)
    monkeypatch.setenv("FAKE_NORMALIZER_RENDER", json.dumps(render))
    return str(script)


async def _commit(stack, *, witnessed: bool) -> tuple[str, dict]:
    payload = _payload(tenant_id=get_settings().archive_primary_tenant_id, machine_id="cinder", epoch=uuid4())
    session_id = str(uuid4())
    payload["session_id"] = session_id
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
        assert [(row["outcome"], row["detail"], row["normalizer_revision"]) for row in results] == [("match", None, "fake-rev")]

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
        assert worker.revision == "fake-rev"
        await stack.catalog.call("render.shadow.record.v2", {"results": results})

        async def candidates(revision):
            listed = await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": revision})
            return [row["session_id"] for row in listed["sessions"]]

        assert await candidates("fake-rev") == []
        assert await candidates(None) == []
        assert await candidates("next-rev") == [session_id]

        await stack.catalog.call("render.shadow.record.v2", {"results": [{**results[0], "outcome": "error"}]})
        assert await candidates("fake-rev") == [session_id]


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
        await stack.catalog.call("render.shadow.record.v2", {"results": results})
        monkeypatch.setattr(shadow_store, "SHADOW_SESSION_COOLDOWN", shadow_store.timedelta(0))
        listed = await stack.catalog.call("render.shadow.candidates.v2", {"limit": 10, "revision": None})
        assert listed["sessions"] == []


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
