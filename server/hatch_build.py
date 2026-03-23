from __future__ import annotations

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class EditableFrontendHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            build_data["force_include_editable"] = {
                "README.md": "zerg/_frontend_dist/.editable-placeholder"
            }


def get_build_hook() -> type[BuildHookInterface]:
    return EditableFrontendHook
