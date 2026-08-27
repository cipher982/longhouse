"""Ship a golden transcript through the API, unship it byte-for-byte.

The v1 ``/agents/ingest`` route this file used to drive is gone: a Runtime Host
accepts transcripts only through storage-v2, and ``/agents/sessions/{id}/export``
replays sealed raw records instead of reassembling rows out of the archive. The
invariant outlived the protocol change -- what a machine ships is exactly what
comes back -- so it is asserted here against a real catalog and real objects on
disk.

Raw export never consults a parser: it streams the immutable record bytes in
source order and adds nothing but the newline a record does not already carry.
Both golden fixtures therefore ride the same provider; they are here for their
byte shape, not for which parser produced them.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401

DEVICE_ID = "ship-unship"


def _fixture_path(provider: str) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "engine" / "tests" / "fixtures" / "golden" / provider / "basic.jsonl"


@pytest.mark.parametrize("fixture", ["claude", "codex"])
def test_ship_unship_roundtrip_is_byte_exact(live_catalog, live_catalog_client, fixture: str):
    """Every byte of a shipped transcript comes back out of export unchanged."""

    expected = _fixture_path(fixture).read_bytes()
    lines = tuple(expected.decode("utf-8").splitlines())
    assert lines, "the golden fixture should carry transcript lines"

    owner = live_catalog.create_user(f"owner@{fixture}-roundtrip.test")
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id = uuid4()

    shipped = live_catalog_client.post(
        "/agents/storage/v2/envelopes",
        json=live_catalog.envelope_body(session_id=session_id, device_id=DEVICE_ID, texts=lines),
        headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert shipped.status_code == 200, shipped.text

    exported = live_catalog_client.get(
        f"/agents/sessions/{session_id}/export",
        headers={"X-Agents-Token": token},
    )
    assert exported.status_code == 200, exported.text
    assert exported.content == expected
