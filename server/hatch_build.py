from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

STAGED_IDENTITY_REL = "zerg/build_identity.json"


class LonghouseBuildHook(BuildHookInterface):
    """Custom Hatch hook.

    1. Editable installs get a placeholder for the frontend dist path so
       the package imports cleanly without a prebuilt web bundle.
    2. Any non-editable build (wheel, sdist) demands that
       `server/zerg/build_identity.json` already exists.
       `scripts/build/generate_build_identity.py` must be run first.
       No fallback — missing identity = loud failure.
    """

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            build_data["force_include_editable"] = {
                "README.md": "zerg/_frontend_dist/.editable-placeholder"
            }
            return

        identity_path = Path(self.root) / STAGED_IDENTITY_REL
        if not identity_path.is_file():
            raise RuntimeError(
                f"build identity missing at {identity_path.resolve()}. "
                "Run `python3 scripts/build/generate_build_identity.py` before building "
                "a wheel or sdist. See docs/specs/release-and-build-identity.md."
            )

def get_build_hook() -> type[BuildHookInterface]:
    return LonghouseBuildHook
