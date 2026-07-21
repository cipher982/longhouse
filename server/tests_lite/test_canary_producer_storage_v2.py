"""Canary producer storage-v2 bootstrap contract tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_producer():
    path = Path(__file__).resolve().parents[2] / "scripts" / "canary" / "producer.py"
    spec = importlib.util.spec_from_file_location("canary_producer_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canary_producer_source_never_calls_legacy_ingest():
    producer_path = Path(__file__).resolve().parents[2] / "scripts" / "canary" / "producer.py"
    source = producer_path.read_text()
    assert "/api/agents/storage/v2/capabilities" in source
    assert "lane_header" in source
    assert "build_canary_bootstrap_envelope" in source
    assert 'f"{base_url}/api/agents/ingest"' not in source
    assert "json=ingest_payload" not in source
    # Runtime ticks remain the SSE wake path.
    assert "/api/agents/runtime/events/batch" in source
    assert "_binding_event" in source
    assert "_runtime_event" in source
    # Secrets must stay out of logs.
    assert "print(agents_token" not in source
    assert "print(canary_token" not in source


def test_canary_producer_bootstrap_fail_closed_on_missing_cutover(monkeypatch):
    producer = _load_producer()

    class _Resp:
        def __init__(self, status_code: int, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    class _Client:
        def get(self, url, headers=None, timeout=None):
            assert url.endswith("/api/agents/storage/v2/capabilities")
            assert headers["X-Agents-Token"] == "token"
            return _Resp(200, {"protocol_version": 2, "cutover": False})

        def post(self, *args, **kwargs):
            raise AssertionError("must not POST envelopes when cutover is false")

    exits: list[int] = []

    def _fake_exit(code: int):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(producer.sys, "exit", _fake_exit)
    try:
        producer._bootstrap_storage_v2(
            _Client(),
            base_url="https://example.test",
            agents_token="token",
            session_id="a776f692-7fb8-44a7-9574-e347fa29b88e",
        )
        raise AssertionError("bootstrap must fail closed")
    except SystemExit as exc:
        assert int(exc.code) == 3
    assert exits == [3]


def test_canary_producer_bootstrap_posts_live_lane_and_validates_receipt(monkeypatch):
    producer = _load_producer()
    session_id = "a776f692-7fb8-44a7-9574-e347fa29b88e"
    envelope = producer._storage_v2_wire.build_canary_bootstrap_envelope(
        tenant_id="tenant-a",
        machine_id="canary-host",
        session_id=session_id,
    )
    posts: list[dict] = []

    class _Resp:
        def __init__(self, status_code: int, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    class _Client:
        def get(self, url, headers=None, timeout=None):
            return _Resp(
                200,
                {
                    "protocol_version": 2,
                    "cutover": True,
                    "tenant_id": "tenant-a",
                    "machine_id": "canary-host",
                    "ingest_path": "/api/agents/storage/v2/envelopes",
                    "lane_header": "X-Longhouse-Storage-Lane",
                },
            )

        def post(self, url, headers=None, json=None, timeout=None):
            posts.append({"url": url, "headers": headers, "json": json})
            assert "/api/agents/ingest" not in url
            return _Resp(
                200,
                {
                    "v": 2,
                    "envelope_id": envelope["expected_envelope_id"],
                    "object_hash": "0" * 64,
                    "commit_seq": "1",
                    "raw_state": "durable",
                    "render_state": "ready",
                    "media_state": "complete",
                    "missing_media_hashes": [],
                },
            )

    envelope_id = producer._bootstrap_storage_v2(
        _Client(),
        base_url="https://example.test",
        agents_token="token",
        session_id=session_id,
    )
    assert envelope_id == envelope["expected_envelope_id"]
    assert len(posts) == 1
    assert posts[0]["url"] == "https://example.test/api/agents/storage/v2/envelopes"
    assert posts[0]["headers"]["X-Longhouse-Storage-Lane"] == "live"
    assert posts[0]["headers"]["X-Agents-Token"] == "token"
    assert posts[0]["json"]["expected_envelope_id"] == envelope["expected_envelope_id"]
    assert posts[0]["json"]["session"]["hidden_from_default_timeline"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("render_state", "pending"),
        ("object_hash", "not-a-sha256"),
        ("commit_seq", "01"),
    ],
)
def test_canary_producer_bootstrap_rejects_noncanonical_receipt(field, value):
    producer = _load_producer()
    session_id = "a776f692-7fb8-44a7-9574-e347fa29b88e"
    envelope = producer._storage_v2_wire.build_canary_bootstrap_envelope(
        tenant_id="tenant-a",
        machine_id="canary-host",
        session_id=session_id,
    )

    class _Resp:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def get(self, *_args, **_kwargs):
            return _Resp(
                {
                    "protocol_version": 2,
                    "cutover": True,
                    "tenant_id": "tenant-a",
                    "machine_id": "canary-host",
                    "ingest_path": "/api/agents/storage/v2/envelopes",
                    "lane_header": "X-Longhouse-Storage-Lane",
                }
            )

        def post(self, *_args, **_kwargs):
            receipt = {
                "v": 2,
                "envelope_id": envelope["expected_envelope_id"],
                "object_hash": "0" * 64,
                "commit_seq": "1",
                "raw_state": "durable",
                "render_state": "ready",
                "media_state": "complete",
                "missing_media_hashes": [],
            }
            receipt[field] = value
            return _Resp(receipt)

    with pytest.raises(SystemExit) as stopped:
        producer._bootstrap_storage_v2(
            _Client(),
            base_url="https://example.test",
            agents_token="token",
            session_id=session_id,
        )
    assert int(stopped.value.code) == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ingest_path", "/api/agents/ingest"),
        ("ingest_path", "/api/agents/storage/v2/envelopes/alternate"),
        ("lane_header", "Authorization"),
    ],
)
def test_canary_producer_bootstrap_rejects_unexpected_advertised_route(field, value):
    producer = _load_producer()

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            capabilities = {
                "protocol_version": 2,
                "cutover": True,
                "tenant_id": "tenant-a",
                "machine_id": "canary-host",
                "ingest_path": "/api/agents/storage/v2/envelopes",
                "lane_header": "X-Longhouse-Storage-Lane",
            }
            capabilities[field] = value
            return capabilities

    class _Client:
        def get(self, *_args, **_kwargs):
            return _Resp()

        def post(self, *_args, **_kwargs):
            raise AssertionError("must not POST to an unexpected advertised route")

    with pytest.raises(SystemExit) as stopped:
        producer._bootstrap_storage_v2(
            _Client(),
            base_url="https://example.test",
            agents_token="token",
            session_id="a776f692-7fb8-44a7-9574-e347fa29b88e",
        )
    assert int(stopped.value.code) == 3
