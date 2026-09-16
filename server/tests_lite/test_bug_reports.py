import json

import pytest

from zerg.services.bug_reports import BugReportUpload
from zerg.services.bug_reports import create_bug_report
from zerg.services.bug_reports import read_manifest
from zerg.services.bug_reports import read_report_file


def test_bug_report_is_atomically_published_and_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_BUG_REPORT_ROOT", str(tmp_path / "reports"))
    bundle = create_bug_report(
        owner_id=7,
        description="The transcript went blank after reconnect.",
        context_json=json.dumps({"session_id": "session-1", "recent_actions": ["reconnect"]}),
        source_session_id="session-1",
        uploads=[BugReportUpload(filename="screen.png", mime_type="image/png", data=b"png-bytes")],
    )

    manifest = read_manifest(bundle.report_id, owner_id=7)
    assert manifest["report_id"] == bundle.report_id
    assert {entry["kind"] for entry in manifest["files"]} == {"description", "context", "image"}
    path, entry = read_report_file(bundle.report_id, owner_id=7, filename="image-0.png")
    assert path.read_bytes() == b"png-bytes"
    assert entry["byte_size"] == len(b"png-bytes")
    with pytest.raises(FileNotFoundError):
        read_manifest(bundle.report_id, owner_id=8)


def test_bug_report_rejects_unbounded_or_untrusted_images(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_BUG_REPORT_ROOT", str(tmp_path / "reports"))
    with pytest.raises(Exception, match="unsupported report image type"):
        create_bug_report(
            owner_id=7,
            description="bad image",
            context_json="{}",
            source_session_id=None,
            uploads=[BugReportUpload(filename="x.svg", mime_type="image/svg+xml", data=b"<svg />")],
        )
