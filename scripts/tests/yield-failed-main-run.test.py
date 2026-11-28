#!/usr/bin/env python3
"""A failed main run yields to a newer waiting main run, and only then."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("yield_failed_main_run", ROOT / "scripts" / "ci" / "yield_failed_main_run.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules["yield_failed_main_run"] = module
SPEC.loader.exec_module(module)

API = "https://api.example"
REPO = "owner/repo"


class FakeApi:
    def __init__(self, waiting: dict[str, list[dict]], fail_on: str | None = None):
        self.waiting = waiting
        self.fail_on = fail_on
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str) -> dict:
        self.calls.append((method, url))
        if self.fail_on and self.fail_on in url:
            raise OSError("api down")
        if method == "POST":
            return {}
        if url.endswith("/actions/runs/100"):
            return {"workflow_id": 7, "run_number": 50}
        status = url.split("status=")[1].split("&")[0]
        return {"workflow_runs": self.waiting.get(status, [])}

    @property
    def cancelled(self) -> bool:
        return ("POST", f"{API}/repos/{REPO}/actions/runs/100/cancel") in self.calls


class YieldTest(unittest.TestCase):
    def setUp(self):
        self.env = {"GITHUB_API_URL": API, "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "100"}
        self._saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_a_newer_waiting_commit_takes_over(self):
        api = FakeApi({"pending": [{"id": 101, "run_number": 51}]})
        self.assertEqual(module.main(api), 0)
        self.assertTrue(api.cancelled)

    def test_with_nothing_waiting_the_run_finishes(self):
        api = FakeApi({})
        self.assertEqual(module.main(api), 0)
        self.assertFalse(api.cancelled)

    def test_an_older_waiting_run_is_not_a_reason(self):
        api = FakeApi({"queued": [{"id": 99, "run_number": 49}]})
        module.main(api)
        self.assertFalse(api.cancelled)

    def test_an_api_failure_never_fails_the_step(self):
        api = FakeApi({}, fail_on="status=pending")
        self.assertEqual(module.main(api), 0)
        self.assertFalse(api.cancelled)


if __name__ == "__main__":
    unittest.main()
