from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest


def _fixture_path(provider: str) -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "apps" / "engine" / "tests" / "fixtures" / "golden" / provider / "basic.jsonl"


def _read_lines_with_offsets(path: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    with path.open("rb") as fh:
        offset = 0
        for raw in fh:
            rows.append((offset, raw.rstrip(b"\r\n").decode("utf-8")))
            offset += len(raw)
    return rows


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    v = value
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extract_fixture_events(provider: str, line_rows: list[tuple[int, str]], source_path: str) -> list[EventIngest]:
    events: list[EventIngest] = []
    for offset, line in line_rows:
        obj = json.loads(line)
        if provider == "claude":
            typ = obj.get("type")
            if typ not in {"user", "assistant"}:
                continue
            role = typ
            content_text = None
            message = obj.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                content_text = content
            elif isinstance(content, list):
                texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                if texts:
                    content_text = "\n".join(t for t in texts if t)
            events.append(
                EventIngest(
                    role=role,
                    content_text=content_text,
                    timestamp=_parse_ts(obj.get("timestamp")),
                    source_path=source_path,
                    source_offset=offset,
                    raw_json=line,
                )
            )
        elif provider == "codex":
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            content_text = None
            texts = []
            for item in payload.get("content") or []:
                if not isinstance(item, dict):
                    continue
                t = item.get("text")
                if t:
                    texts.append(t)
            if texts:
                content_text = "\n".join(texts)
            events.append(
                EventIngest(
                    role=role,
                    content_text=content_text,
                    timestamp=_parse_ts(obj.get("timestamp")),
                    source_path=source_path,
                    source_offset=offset,
                    raw_json=line,
                )
            )
    return events


def _roundtrip_fixture(tmp_path, provider: str) -> None:
    db_path = tmp_path / f"roundtrip_{provider}.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)

    fixture = _fixture_path(provider)
    source_path = str(fixture)
    line_rows = _read_lines_with_offsets(fixture)
    source_lines = [
        SourceLineIngest(source_path=source_path, source_offset=offset, raw_json=line)
        for offset, line in line_rows
    ]
    events = _extract_fixture_events(provider, line_rows, source_path)
    assert events, f"fixture for {provider} must produce at least one event"

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                provider=provider,
                environment="test",
                project="roundtrip",
                device_id="test-device",
                cwd="/tmp/roundtrip",
                git_repo=None,
                git_branch=None,
                started_at=_parse_ts(json.loads(line_rows[0][1]).get("timestamp")),
                ended_at=_parse_ts(json.loads(line_rows[-1][1]).get("timestamp")),
                provider_session_id="fixture",
                events=events,
                source_lines=source_lines,
            )
        )

        exported = store.export_session_jsonl(result.session_id)
        assert exported is not None
        exported_bytes, _session = exported

    expected = fixture.read_text(encoding="utf-8")
    assert exported_bytes.decode("utf-8") == expected


def test_claude_ingest_export_roundtrip(tmp_path):
    _roundtrip_fixture(tmp_path, "claude")


def test_codex_ingest_export_roundtrip(tmp_path):
    _roundtrip_fixture(tmp_path, "codex")
