"""Tests for the iMessage connector tools."""

import sqlite3
import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.tools.builtin import imessage_tools
from zerg.tools.builtin.imessage_tools import list_imessage_messages
from zerg.tools.builtin.imessage_tools import send_imessage


@pytest.fixture
def force_macos(monkeypatch):
    """Force platform.system() to return Darwin for mac-only helpers."""

    def _apply():
        monkeypatch.setattr(imessage_tools.platform, "system", lambda: "Darwin")

    _apply()
    return _apply


class TestSendImessage:
    """Tests for the send_imessage tool."""

    def test_requires_macos(self):
        with patch("zerg.tools.builtin.imessage_tools.platform.system", return_value="Linux"):
            result = send_imessage("+15551234567", "hello")
            assert result["ok"] is False
            assert "macOS" in result["user_message"]

    def test_validates_inputs(self, force_macos, monkeypatch):
        monkeypatch.setattr(imessage_tools.shutil, "which", lambda _: "/usr/bin/osascript")
        monkeypatch.setattr(imessage_tools.subprocess, "run", MagicMock())

        result = send_imessage("", "hi")
        assert result["ok"] is False

        result = send_imessage("+15551234567", "")
        assert result["ok"] is False

        long_text = "x" * (imessage_tools.MAX_MESSAGE_LENGTH + 1)
        result = send_imessage("+15551234567", long_text)
        assert result["ok"] is False

    def test_missing_osascript(self, force_macos, monkeypatch):
        monkeypatch.setattr(imessage_tools.shutil, "which", lambda _: None)
        result = send_imessage("+15551234567", "hello")
        assert result["ok"] is False
        assert "osascript" in result["user_message"]

    def test_successful_send(self, force_macos, monkeypatch):
        monkeypatch.setattr(imessage_tools.shutil, "which", lambda _: "/usr/bin/osascript")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="sent", stderr="")
        mock_run = MagicMock(return_value=completed)
        monkeypatch.setattr(imessage_tools.subprocess, "run", mock_run)

        result = send_imessage("+15551234567", "hello world", chat_guid="iMessage;-;+15551234567")

        assert result["ok"] is True
        assert result["data"]["recipient"] == "+15551234567"
        assert result["data"]["chat_guid"] == "iMessage;-;+15551234567"
        mock_run.assert_called_once()

    def test_process_error(self, force_macos, monkeypatch):
        monkeypatch.setattr(imessage_tools.shutil, "which", lambda _: "/usr/bin/osascript")
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="failure")
        monkeypatch.setattr(imessage_tools.subprocess, "run", MagicMock(return_value=completed))

        result = send_imessage("+15551234567", "hello")
        assert result["ok"] is False
        assert "failure" in result["user_message"]

    def test_timeout(self, force_macos, monkeypatch):
        monkeypatch.setattr(imessage_tools.shutil, "which", lambda _: "/usr/bin/osascript")

        def _timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=10)

        monkeypatch.setattr(imessage_tools.subprocess, "run", _timeout)
        result = send_imessage("+15551234567", "hi", timeout_seconds=1.0)
        assert result["ok"] is False
        assert "Timed out" in result["user_message"]


class TestListImessageMessages:
    """Tests for the list_imessage_messages tool."""

    def _create_temp_db(self, tmp_path: Path):
        """Create a minimal chat.db compatible structure for testing."""
        db_path = tmp_path / "chat.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                is_from_me INTEGER,
                date INTEGER,
                date_read INTEGER,
                handle_id INTEGER,
                service TEXT,
                attributedBody BLOB
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_identifier TEXT,
                guid TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT
            )
            """
        )

        handle_id = conn.execute(
            "INSERT INTO handle (id) VALUES (?)",
            ("+15551234567",),
        ).lastrowid
        chat_id = conn.execute(
            "INSERT INTO chat (chat_identifier, guid) VALUES (?, ?)",
            ("+15551234567", "iMessage;-;+15551234567"),
        ).lastrowid

        def insert_message(text: str, is_from_me: int, minutes_offset: int, handle: int) -> int:
            timestamp = imessage_tools._to_apple_timestamp(
                datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_offset)
            )
            message_id = conn.execute(
                """
                INSERT INTO message (text, is_from_me, date, date_read, handle_id, service)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (text, is_from_me, timestamp, None, handle, "iMessage"),
            ).lastrowid
            conn.execute(
                "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )
            return message_id

        from_me_id = insert_message("Outbound", 1, 20, handle_id)
        inbound_id = insert_message("Inbound", 0, 10, handle_id)

        conn.commit()
        conn.close()
        return db_path, inbound_id, from_me_id

    def test_requires_macos(self, tmp_path):
        db_path, *_ = self._create_temp_db(tmp_path)
        with patch("zerg.tools.builtin.imessage_tools.platform.system", return_value="Linux"):
            with patch("zerg.tools.builtin.imessage_tools.IMESSAGE_DB_PATH", db_path):
                result = list_imessage_messages()
                assert result["ok"] is False

    def test_reads_messages(self, force_macos, tmp_path, monkeypatch):
        db_path, inbound_id, from_me_id = self._create_temp_db(tmp_path)
        monkeypatch.setattr(imessage_tools, "IMESSAGE_DB_PATH", Path(db_path))

        result = list_imessage_messages(limit=5)
        assert result["ok"] is True
        assert len(result["data"]["messages"]) == 1  # exclude outbound by default
        assert result["data"]["messages"][0]["row_id"] == inbound_id
        assert result["data"]["messages"][0]["text"] == "Inbound"
        assert result["data"]["latest_row_id"] == inbound_id

        # Include outbound messages
        result_all = list_imessage_messages(include_outgoing=True, limit=5)
        assert len(result_all["data"]["messages"]) == 2

        # Filter by since_row_id
        result_since = list_imessage_messages(include_outgoing=True, since_row_id=from_me_id)
        assert len(result_since["data"]["messages"]) == 1
        assert result_since["data"]["messages"][0]["row_id"] > from_me_id

    def test_missing_database(self, force_macos, monkeypatch, tmp_path):
        missing_path = tmp_path / "missing.db"
        monkeypatch.setattr(imessage_tools, "IMESSAGE_DB_PATH", missing_path)

        result = list_imessage_messages()
        assert result["ok"] is False
        assert "not found" in result["user_message"]

    def test_permission_error(self, force_macos, monkeypatch, tmp_path):
        db_path, *_ = self._create_temp_db(tmp_path)

        def _copy_fail(src, dst, *, follow_symlinks=True):
            raise PermissionError("no access")

        monkeypatch.setattr(imessage_tools, "IMESSAGE_DB_PATH", Path(db_path))
        monkeypatch.setattr(imessage_tools.shutil, "copy2", _copy_fail)

        result = list_imessage_messages()
        assert result["ok"] is False
        assert "Permission" in result["user_message"]
