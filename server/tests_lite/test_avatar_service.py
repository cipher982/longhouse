from pathlib import Path

from zerg.services.avatar_service import _static_base_dir


def test_static_base_dir_uses_repo_root_for_local_layout():
    module_path = Path("/tmp/zerg/server/zerg/services/avatar_service.py")

    assert _static_base_dir(module_path) == module_path.resolve().parents[3]


def test_static_base_dir_uses_app_root_for_runtime_layout():
    module_path = Path("/app/zerg/services/avatar_service.py")

    assert _static_base_dir(module_path) == Path("/app")
