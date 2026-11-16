from __future__ import annotations

import json

import httpx

from zerg.qa import tenant_evidence as te
from zerg.qa.wait_kind import LONGHOUSE_INGEST
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import expired


def test_only_an_expired_ingest_wait_is_captured() -> None:
    assert te.should_capture(expired(RuntimeError("x"), LONGHOUSE_INGEST))
    assert not te.should_capture(expired(RuntimeError("x"), PROVIDER_TURN))
    assert not te.should_capture(RuntimeError("x"))
    assert not te.should_capture(None)


def test_capture_writes_every_read_and_survives_a_failing_one(tmp_path, monkeypatch) -> None:
    sid = "11111111-1111-1111-1111-111111111111"
    seen: list[str] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        assert headers == {"X-Agents-Token": "tok"}
        seen.append(url.split("://", 1)[1].split("/", 1)[1])
        request = httpx.Request("GET", url)
        if url.endswith("/projection"):
            return httpx.Response(503, json={"detail": "down"}, request=request)
        if url.endswith("/objects/manifest"):
            return httpx.Response(200, json={"found": True, "objects": [{"id": 1}], "next_cursor": None}, request=request)
        return httpx.Response(200, json={"ok": url}, request=request)

    monkeypatch.setattr(te.httpx, "get", fake_get)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "engine.log.2026-11-16").write_text("Shipped storage-v2 source envelope\n")

    path = te.capture_tenant_evidence(url="http://host/", token="tok", session_id=sid, artifact_root=tmp_path, engine_log_dir=logs)

    assert path == tmp_path / te.FILE_NAME
    data = json.loads(path.read_text())
    assert data["ingest_receipts_raw_object_manifest"]["objects"] == [{"id": 1}]
    assert data["ingest_receipts_raw_object_manifest"]["found"] is True
    assert data["projection_tail"]["error"] == "HTTP 503"
    assert "Shipped storage-v2" in data["engine_log_tail"]["tail"]
    assert "server_log_tail" in data
    assert any(p.endswith(f"storage/v2/sessions/{sid}/events") for p in seen)


def test_capture_never_raises(tmp_path) -> None:
    assert te.capture_tenant_evidence(url="http://x", token="t", session_id="s", artifact_root=tmp_path / "missing") is None
