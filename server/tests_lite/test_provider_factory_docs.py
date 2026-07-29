from __future__ import annotations

from pathlib import Path

from zerg.qa.repo_root import default_repo_root


RETIRED_PROVIDER_FACTORY_SPECS = (
    "executable-provider-capability-contract-epic.md",
    "provider-release-proof-roadmap.md",
    "provider-build-matrix.md",
    "provider-automation-factory-completion.md",
)


def test_retired_provider_factory_specs_are_deleted_and_unreferenced() -> None:
    root = default_repo_root()
    canonical = root / "docs/specs/provider-factory-coherence.md"

    for name in RETIRED_PROVIDER_FACTORY_SPECS:
        assert not (root / "docs/specs" / name).exists()

    references: list[str] = []
    for path in sorted((root / "docs").rglob("*.md")):
        if path == canonical:
            continue
        text = path.read_text(encoding="utf-8")
        for name in RETIRED_PROVIDER_FACTORY_SPECS:
            if name in text:
                references.append(f"{path.relative_to(root)} -> {name}")

    assert references == []
