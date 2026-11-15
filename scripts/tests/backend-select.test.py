#!/usr/bin/env python3
"""scripts/ci/backend_select.py: import-graph test selection with conservative full-suite fallbacks."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("backend_select", ROOT / "scripts" / "ci" / "backend_select.py")
assert SPEC and SPEC.loader
bs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bs
SPEC.loader.exec_module(bs)


def write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tree(tmp: str) -> Path:
    root = Path(tmp)
    write(root, "server/zerg/__init__.py")
    write(root, "server/zerg/core.py", "X = 1\n")
    write(root, "server/zerg/mid.py", "from zerg.core import X\n")
    write(root, "server/zerg/leaf.py", "from zerg import mid\n")
    write(root, "server/zerg/lonely.py", "Y = 2\n")
    write(root, "server/zerg/patched.py", "Z = 3\n")
    write(root, "server/zerg/models/__init__.py")
    write(root, "server/tests_lite/__init__.py")
    write(root, "server/tests_lite/_helper.py", "from zerg.lonely import Y\n")
    write(root, "server/tests_lite/test_leaf.py", "from zerg.leaf import *\n")
    write(root, "server/tests_lite/test_core_direct.py", "import zerg.core\n")
    write(root, "server/tests_lite/test_lonely_via_helper.py", "from tests_lite._helper import Y\n")
    write(root, "server/tests_lite/test_patch.py", 'def test():\n    patch("zerg.patched.thing")\n')
    write(root, "server/tests_lite/test_reads_data.py", 'P = "schemas_dir/thing/data.json"\n')
    for i in range(12):
        write(root, f"server/tests_lite/test_filler_{i}.py", "x = 1\n")
    return root


def test_transitive_importers_are_selected_and_unrelated_tests_are_not() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sel = bs.select(tree(tmp), ["server/zerg/core.py"])
        assert not sel.full
        assert sel.tests == ["tests_lite/test_core_direct.py", "tests_lite/test_leaf.py"], sel
        assert "tests_lite/test_filler_0.py" not in sel.tests


def test_helper_modules_in_the_test_tree_propagate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sel = bs.select(tree(tmp), ["server/zerg/lonely.py"])
        assert sel.tests == ["tests_lite/test_lonely_via_helper.py"], sel


def test_string_references_to_a_module_select_the_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sel = bs.select(tree(tmp), ["server/zerg/patched.py"])
        assert sel.tests == ["tests_lite/test_patch.py"], sel


def test_non_python_files_select_tests_that_name_the_path_or_its_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sel = bs.select(tree(tmp), ["schemas_dir/thing/other.json"])
        assert sel.tests == ["tests_lite/test_reads_data.py"], sel


def test_shared_inputs_fall_back_to_the_full_suite_with_a_reason() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = tree(tmp)
        for path in (
            "schemas/managed_providers.yml",
            "config/models.json",
            "server/zerg/models/session.py",
            "server/tests_lite/conftest.py",
            "server/tests_lite/fixtures/a.json",
            "server/tests_lite/data.json",
            "server/pyproject.toml",
            "server/zerg/database.py",
        ):
            sel = bs.select(root, [path])
            assert sel.full and path in sel.reason, (path, sel)


def test_unparseable_changed_file_is_a_full_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = tree(tmp)
        write(root, "server/zerg/core.py", "def (:\n")
        assert bs.select(root, ["server/zerg/core.py"]).full


def test_a_selection_over_half_the_suite_is_the_full_suite() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = tree(tmp)
        for i in range(12):
            write(root, f"server/tests_lite/test_filler_{i}.py", "import zerg.core\n")
        sel = bs.select(root, ["server/zerg/core.py"])
        assert sel.full and "reach the change" in sel.reason


def test_a_changed_test_file_selects_itself() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sel = bs.select(tree(tmp), ["server/tests_lite/test_patch.py"])
        assert sel.tests == ["tests_lite/test_patch.py"], sel


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"backend-select tests passed ({len(tests)} scenarios)")
