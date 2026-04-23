from __future__ import annotations

from pathlib import Path

import pytest

from zerg.services.machine_state import load_machine_state
from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import normalize_zerg_url
from zerg.services.shipper.token import save_machine_name
from zerg.services.shipper.token import save_zerg_url


def test_normalize_zerg_url_accepts_http_and_https():
    assert normalize_zerg_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert normalize_zerg_url("https://demo.longhouse.test") == "https://demo.longhouse.test"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "ftp://example.com",
        "https://<typer.models.OptionInfo object at 0x1234>",
        "<typer.models.OptionInfo object at 0x1234>",
    ],
)
def test_normalize_zerg_url_rejects_invalid_values(raw):
    assert normalize_zerg_url(raw) is None


def test_get_zerg_url_ignores_invalid_persisted_value(tmp_path: Path):
    machine_dir = tmp_path / "machine"
    machine_dir.mkdir()
    (machine_dir / "state.json").write_text('{"runtime_url":"https://<typer.models.OptionInfo object at 0x1234>"}')

    assert get_zerg_url(tmp_path) is None


def test_save_zerg_url_writes_canonical_state_and_journal(tmp_path: Path):
    save_zerg_url("https://demo.longhouse.test", tmp_path)

    state = load_machine_state(tmp_path)
    assert state is not None
    assert state.runtime_url == "https://demo.longhouse.test"

    journal_path = tmp_path / "machine" / "state-journal.jsonl"
    journal = journal_path.read_text()
    assert "https://demo.longhouse.test" in journal
    assert "shipper-save-url" in journal


def test_save_zerg_url_rejects_invalid_value(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid Longhouse URL"):
        save_zerg_url("https://<typer.models.OptionInfo object at 0x1234>", tmp_path)


def test_save_machine_name_writes_canonical_state_and_journal(tmp_path: Path):
    save_machine_name("Cinder Local", tmp_path)

    state = load_machine_state(tmp_path)
    assert state is not None
    assert state.machine_name == "Cinder-Local"

    journal_path = tmp_path / "machine" / "state-journal.jsonl"
    journal = journal_path.read_text()
    assert "Cinder-Local" in journal
    assert "shipper-save-machine-name" in journal
