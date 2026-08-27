from __future__ import annotations

import os

import pytest

from zerg.services.factory_assurance_title_binding import FACTORY_ASSURANCE_ENVIRONMENT
from zerg.services.factory_assurance_title_binding import FACTORY_ASSURANCE_MODE_ENV
from zerg.services.factory_assurance_title_binding import FACTORY_ASSURANCE_TITLE_BASE_URL_ENV
from zerg.services.factory_assurance_title_binding import FACTORY_ASSURANCE_TITLE_TOKEN_FILE_ENV
from zerg.services.factory_assurance_title_binding import load_factory_assurance_title_binding


def _environment(token_file, *, base_url="http://127.0.0.1:43123/v1"):
    return {
        "ENVIRONMENT": FACTORY_ASSURANCE_ENVIRONMENT,
        FACTORY_ASSURANCE_MODE_ENV: "1",
        FACTORY_ASSURANCE_TITLE_BASE_URL_ENV: base_url,
        FACTORY_ASSURANCE_TITLE_TOKEN_FILE_ENV: str(token_file),
    }


def test_factory_title_binding_is_absent_without_any_assurance_configuration():
    assert load_factory_assurance_title_binding({}) is None


def test_factory_title_binding_reads_generation_from_owner_only_file(tmp_path):
    token_file = tmp_path / "title-token"
    token_file.write_text("generation-a-token")
    token_file.chmod(0o600)

    binding = load_factory_assurance_title_binding(_environment(token_file))
    assert binding is not None
    assert binding.base_url == "http://127.0.0.1:43123/v1"
    assert binding.read_token() == "generation-a-token"

    token_file.write_text("generation-b-token")
    token_file.chmod(0o600)
    assert binding.read_token() == "generation-b-token"
    assert str(token_file) not in binding.credential_binding


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"ENVIRONMENT": "production"}, f"ENVIRONMENT={FACTORY_ASSURANCE_ENVIRONMENT}"),
        ({FACTORY_ASSURANCE_MODE_ENV: "0"}, "explicit assurance mode"),
        ({FACTORY_ASSURANCE_TITLE_BASE_URL_ENV: "https://127.0.0.1:443/v1"}, "loopback HTTP URL"),
        ({FACTORY_ASSURANCE_TITLE_BASE_URL_ENV: "http://example.com:80/v1"}, "loopback"),
    ],
)
def test_factory_title_binding_rejects_missing_or_non_loopback_gates(tmp_path, override, message):
    token_file = tmp_path / "title-token"
    token_file.write_text("generation-a-token")
    token_file.chmod(0o600)
    environment = _environment(token_file)
    environment.update(override)
    with pytest.raises(ValueError, match=message):
        load_factory_assurance_title_binding(environment)


def test_factory_title_binding_rejects_relative_or_exposed_token_file(tmp_path):
    token_file = tmp_path / "title-token"
    token_file.write_text("generation-a-token")
    token_file.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        load_factory_assurance_title_binding(_environment(token_file))

    environment = _environment(token_file)
    environment[FACTORY_ASSURANCE_TITLE_TOKEN_FILE_ENV] = os.path.relpath(token_file)
    with pytest.raises(ValueError, match="absolute"):
        load_factory_assurance_title_binding(environment)


def test_factory_title_binding_rejects_symlink_token_file(tmp_path):
    target = tmp_path / "target"
    target.write_text("generation-a-token")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="not a symlink"):
        load_factory_assurance_title_binding(_environment(link))
