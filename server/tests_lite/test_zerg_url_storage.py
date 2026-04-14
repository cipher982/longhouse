from __future__ import annotations

from pathlib import Path

import pytest

from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import normalize_zerg_url
from zerg.services.shipper.token import save_zerg_url


def test_normalize_zerg_url_accepts_http_and_https():
    assert normalize_zerg_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert normalize_zerg_url("https://david010.longhouse.ai") == "https://david010.longhouse.ai"


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
    (tmp_path / "longhouse-url").write_text("https://<typer.models.OptionInfo object at 0x1234>\n")

    assert get_zerg_url(tmp_path) is None


def test_save_zerg_url_rejects_invalid_value(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid Longhouse URL"):
        save_zerg_url("https://<typer.models.OptionInfo object at 0x1234>", tmp_path)
