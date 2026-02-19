"""Tests for the connect CLI command."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from typer.testing import CliRunner

from zerg.cli.main import app

# Use env to disable Rich's terminal detection for consistent output in CI
runner = CliRunner(mix_stderr=False, env={"TERM": "dumb", "NO_COLOR": "1", "COLUMNS": "120"})

_ENGINE = "/usr/local/bin/longhouse-engine"


class TestShipCommand:
    """Tests for the ship command (wraps longhouse-engine ship)."""

    def test_ship_help(self):
        result = runner.invoke(app, ["ship", "--help"])
        assert result.exit_code == 0
        assert "One-shot" in result.output

    def test_ship_execs_engine(self, tmp_path: Path):
        """ship invokes longhouse-engine ship and propagates exit code."""
        mock_proc = MagicMock(spec=subprocess.CompletedProcess)
        mock_proc.returncode = 0

        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.subprocess.run", return_value=mock_proc) as mock_run:
                with patch("zerg.cli.connect.get_zerg_url", return_value="http://localhost:47300"):
                    with patch("zerg.cli.connect.load_token", return_value="tok"):
                        result = runner.invoke(app, ["ship"])

        assert result.exit_code == 0
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == _ENGINE
        assert call_args[1] == "ship"

    def test_ship_file_passes_flag(self, tmp_path: Path):
        """ship --file passes --file to engine."""
        session_file = tmp_path / "session.jsonl"
        session_file.write_text("{}\n")

        mock_proc = MagicMock(spec=subprocess.CompletedProcess)
        mock_proc.returncode = 0

        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.subprocess.run", return_value=mock_proc) as mock_run:
                with patch("zerg.cli.connect.get_zerg_url", return_value="http://localhost:47300"):
                    with patch("zerg.cli.connect.load_token", return_value="tok"):
                        result = runner.invoke(app, ["ship", "--file", str(session_file)])

        assert result.exit_code == 0
        call_args = mock_run.call_args[0][0]
        assert "--file" in call_args
        assert str(session_file) in call_args

    def test_ship_quiet_uses_devnull(self, tmp_path: Path):
        """ship --quiet suppresses both stdout and stderr."""
        session_file = tmp_path / "session.jsonl"
        session_file.write_text("{}\n")

        mock_proc = MagicMock(spec=subprocess.CompletedProcess)
        mock_proc.returncode = 0

        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.subprocess.run", return_value=mock_proc) as mock_run:
                with patch("zerg.cli.connect.get_zerg_url", return_value="http://localhost:47300"):
                    with patch("zerg.cli.connect.load_token", return_value="tok"):
                        runner.invoke(app, ["ship", "--file", str(session_file), "--quiet"])

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs.get("stdout") == subprocess.DEVNULL
        assert call_kwargs.get("stderr") == subprocess.DEVNULL

    def test_ship_propagates_nonzero_exit(self, tmp_path: Path):
        """ship propagates non-zero engine exit code."""
        mock_proc = MagicMock(spec=subprocess.CompletedProcess)
        mock_proc.returncode = 2

        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.subprocess.run", return_value=mock_proc):
                with patch("zerg.cli.connect.get_zerg_url", return_value="http://localhost:47300"):
                    with patch("zerg.cli.connect.load_token", return_value="tok"):
                        result = runner.invoke(app, ["ship"])

        assert result.exit_code == 2

    def test_ship_missing_file_exits_1(self, tmp_path: Path):
        """ship --file with nonexistent path exits 1."""
        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.get_zerg_url", return_value="http://localhost:47300"):
                with patch("zerg.cli.connect.load_token", return_value="tok"):
                    result = runner.invoke(app, ["ship", "--file", str(tmp_path / "nope.jsonl")])

        assert result.exit_code == 1


class TestConnectCommand:
    """Tests for the connect command."""

    def test_connect_help(self):
        result = runner.invoke(app, ["connect", "--help"])
        assert result.exit_code == 0
        assert "Continuous" in result.output

    def test_connect_options(self):
        result = runner.invoke(app, ["connect", "--help"])
        assert "--url" in result.output
        assert "--interval" in result.output
        assert "--claude-dir" in result.output

    def test_connect_poll_warns(self):
        """--poll prints a deprecation warning."""
        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.get_zerg_url", return_value="http://localhost:47300"):
                with patch("zerg.cli.connect.load_token", return_value="tok"):
                    with patch("zerg.cli.connect.save_token"):
                        with patch("zerg.cli.connect.save_zerg_url"):
                            with patch("os.execvpe"):
                                result = runner.invoke(app, ["connect", "--poll"])

        assert "not supported" in result.output.lower() or "warning" in result.output.lower() or "ignored" in result.output.lower()

    def test_connect_debounce_maps_to_flush_ms(self):
        """--debounce maps to --flush-ms in engine args."""
        captured = {}

        def fake_execvpe(prog, args, env):
            captured["args"] = args

        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.get_zerg_url", return_value="http://localhost:47300"):
                with patch("zerg.cli.connect.load_token", return_value="tok"):
                    with patch("zerg.cli.connect.save_token"):
                        with patch("zerg.cli.connect.save_zerg_url"):
                            with patch("os.execvpe", side_effect=fake_execvpe):
                                runner.invoke(app, ["connect", "--debounce", "200"])

        assert "--flush-ms" in captured.get("args", [])
        assert "200" in captured.get("args", [])

    def test_connect_persists_url_before_exec(self):
        """connect persists URL to file before execing engine."""
        saved = {}

        def capture_save_url(url, config_dir=None):
            saved["url"] = url

        with patch("zerg.cli.connect.get_engine_executable", return_value=_ENGINE):
            with patch("zerg.cli.connect.get_zerg_url", return_value=None):
                with patch("zerg.cli.connect.load_token", return_value="tok"):
                    with patch("zerg.cli.connect.save_token"):
                        with patch("zerg.cli.connect.save_zerg_url", side_effect=capture_save_url):
                            with patch("os.execvpe"):
                                runner.invoke(app, ["connect", "--url", "http://custom:1234"])

        assert saved.get("url") == "http://custom:1234"


class TestMainCli:
    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "ship" in result.output
        assert "connect" in result.output

    def test_no_args(self):
        result = runner.invoke(app)
        assert result.exit_code == 0
