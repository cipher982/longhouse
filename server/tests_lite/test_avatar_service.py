import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import UploadFile

from zerg.config import get_settings_unchecked
from zerg.services import avatar_service
from zerg.services import avatar_storage


def test_avatar_storage_uses_canonical_data_root(tmp_path):
    with patch.object(avatar_storage, "get_settings", return_value=SimpleNamespace(data_dir=tmp_path)):
        resolved = avatar_storage.avatar_storage_dir()

    assert resolved == tmp_path / "avatars"
    assert not resolved.exists()


def test_explicit_data_root_supports_immutable_runtime_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_DATA_DIR", str(tmp_path))

    assert get_settings_unchecked().data_dir == tmp_path


def test_explicit_data_root_rejects_relative_paths(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_DATA_DIR", "relative-data")

    with pytest.raises(ValueError, match="absolute"):
        _ = get_settings_unchecked().data_dir


def test_avatar_storage_is_created_on_first_write_not_module_import(tmp_path):
    avatar_root = tmp_path / "static" / "avatars"
    upload = UploadFile(filename="avatar.png", file=io.BytesIO(b"avatar"))
    upload.headers = {"content-type": "image/png"}

    assert not avatar_root.exists()
    with (
        patch.object(avatar_service, "avatar_storage_dir", return_value=avatar_root),
        patch.object(avatar_service, "_process_image", return_value=(b"processed", "png")),
    ):
        url = avatar_service.store_avatar_for_user(upload)

    assert url.startswith("/static/avatars/")
    assert tuple(avatar_root.iterdir())[0].read_bytes() == b"processed"
