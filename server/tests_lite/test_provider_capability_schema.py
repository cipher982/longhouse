from pathlib import Path

import pytest

from zerg.services import provider_capability_schema


def test_schema_path_resolution_falls_back_through_multiple_candidates(monkeypatch, tmp_path: Path) -> None:
    # Regression: the deployed Runtime Host image is not a full repo
    # checkout (docker/runtime.dockerfile copies only server/'s contents
    # into /app), so the local-dev candidate (ROOT / "schemas" / ...) does
    # not exist there. The endpoint's first real deploy 500'd on exactly
    # this -- schemas/managed_providers.yml was never copied into the image
    # at all, and _resolve_schema_path()'s local-checkout candidate was the
    # only one that existed at the time.
    #
    # Review 2026-07-29 (Grok): the original version of this test
    # monkeypatched _resolve_schema_path itself with an equivalent lambda,
    # then asserted the replacement worked -- tautological, it never
    # executed the real function's loop-and-select logic. Patching the
    # narrower _schema_path_candidates() instead lets the real
    # _resolve_schema_path() body actually run against a fabricated list.
    missing_schema = tmp_path / "no-repo-here" / "schemas" / "managed_providers.yml"
    real_schema = tmp_path / "deployed" / "managed_providers.yml"
    real_schema.parent.mkdir(parents=True)
    real_schema.write_text("providers: []\n")

    monkeypatch.setattr(provider_capability_schema, "_schema_path_candidates", lambda: (missing_schema, real_schema))
    resolved = provider_capability_schema._resolve_schema_path()
    assert resolved == real_schema


def test_schema_path_resolution_raises_a_clear_error_when_no_candidate_exists(monkeypatch, tmp_path: Path) -> None:
    # ROOT and PACKAGE_ROOT are the only pieces of real state
    # _resolve_schema_path() depends on for its checkout and wheel
    # candidates; the remaining candidate is an absolute path this dev
    # machine genuinely does not have (it only exists inside the deployed
    # container).
    monkeypatch.setattr(provider_capability_schema, "ROOT", tmp_path / "no-repo-here")
    monkeypatch.setattr(provider_capability_schema, "PACKAGE_ROOT", tmp_path / "no-wheel-here")
    with pytest.raises(FileNotFoundError, match="not found at any known location"):
        provider_capability_schema._resolve_schema_path()


def test_wheel_candidate_resolves_when_no_checkout_or_image_schema_exists(monkeypatch, tmp_path: Path) -> None:
    # The pip-install case this candidate exists for: no repo above the
    # package, no /schemas. Before it was added, every self-hoster's
    # /api/agents/provider-capabilities raised FileNotFoundError.
    package_root = tmp_path / "site-packages" / "zerg"
    wheel_schema = package_root / "_config" / "managed_providers.yml"
    wheel_schema.parent.mkdir(parents=True)
    wheel_schema.write_text("providers: []\n")

    monkeypatch.setattr(provider_capability_schema, "ROOT", tmp_path / "no-repo-here")
    monkeypatch.setattr(provider_capability_schema, "PACKAGE_ROOT", package_root)
    assert provider_capability_schema._resolve_schema_path() == wheel_schema


def test_real_schema_loads_every_declared_assertion() -> None:
    assertions = provider_capability_schema.load_capability_assertions()
    assert assertions
    # Each row carries the vocabulary the projection endpoint joins on.
    for assertion in assertions:
        assert assertion.assertion_id
        assert assertion.scenario_id
        assert assertion.provider
        assert assertion.max_age_seconds > 0
