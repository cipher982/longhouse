#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import shutil
import sys
import tempfile
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "run-readme-tests.py"

spec = importlib.util.spec_from_file_location("run_readme_tests", MODULE_PATH)
assert spec is not None
run_readme_tests = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = run_readme_tests
spec.loader.exec_module(run_readme_tests)

_TEMP_DIRS: list[Path] = []


def write_markdown(text: str) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="longhouse-readme-test-"))
    _TEMP_DIRS.append(temp_dir)
    path = temp_dir / "README.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_bad_json_fails_contract_parse() -> None:
    path = write_markdown(
        """
# Example

```readme-test
{"name": "broken",
```
"""
    )

    try:
        run_readme_tests.extract_blocks(path)
    except run_readme_tests.ReadmeTestError as exc:
        assert "bad readme-test JSON" in str(exc)
    else:
        raise AssertionError("bad readme-test JSON should fail")


def test_unterminated_block_fails_contract_parse() -> None:
    path = write_markdown(
        """
# Example

```readme-test
{"name": "broken"}
"""
    )

    try:
        run_readme_tests.extract_blocks(path)
    except run_readme_tests.ReadmeTestError as exc:
        assert "unterminated readme-test block" in str(exc)
    else:
        raise AssertionError("unterminated readme-test block should fail")


def test_empty_scan_fails_without_explicit_allow_empty() -> None:
    path = write_markdown("# Example\n")

    with redirect_stdout(io.StringIO()):
        assert run_readme_tests.main(["--mode", "smoke", str(path)]) == 1
        assert run_readme_tests.main(["--mode", "smoke", "--allow-empty", str(path)]) == 0


def test_empty_steps_do_not_pass() -> None:
    block = {"name": "empty", "_source": "test", "steps": []}

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        assert run_readme_tests.run_block(block) is False


def test_valid_block_extracts_source() -> None:
    path = write_markdown(
        """
# Example

```readme-test
{
  "name": "ok",
  "steps": ["true"]
}
```
"""
    )

    blocks = run_readme_tests.extract_blocks(path)

    assert len(blocks) == 1
    assert blocks[0]["name"] == "ok"
    assert blocks[0]["_source"] == str(path)


def main() -> int:
    try:
        test_bad_json_fails_contract_parse()
        test_unterminated_block_fails_contract_parse()
        test_empty_scan_fails_without_explicit_allow_empty()
        test_empty_steps_do_not_pass()
        test_valid_block_extracts_source()
        print("readme-tests.test.py: OK")
        return 0
    finally:
        for temp_dir in _TEMP_DIRS:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
