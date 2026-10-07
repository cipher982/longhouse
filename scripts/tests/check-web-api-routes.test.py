#!/usr/bin/env python3
"""Unit tests for scripts/ci/check-web-api-routes.py (no server import)."""

import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_web_api_routes",
    Path(__file__).resolve().parents[1] / "ci" / "check-web-api-routes.py",
)
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

ROUTES = [
    "/api/sessions/{session_id}/pause-requests/{pause_request_id}/response",
    "/api/timeline/sessions/{session_id}/workspace",
    "/api/timeline/sessions",
]

MODULE = """
const TIMELINE_API_PREFIX = "/timeline";
const TIMELINE_SESSIONS_PREFIX = `${TIMELINE_API_PREFIX}/sessions`;
// `/not/a/route` in a comment is ignored
export function list(queryString: string) {
  return request(`${TIMELINE_SESSIONS_PREFIX}${queryString ? `?${queryString}` : ""}`);
}
export function workspace(sessionId: string, qs: string) {
  return request(`${TIMELINE_SESSIONS_PREFIX}/${sessionId}/workspace?${qs}`);
}
"""


class CheckWebApiRoutesTest(unittest.TestCase):
    def test_resolves_constants_params_and_query_strings(self):
        self.assertEqual(
            sorted(set(check.web_paths(MODULE))),
            ["/api/timeline/sessions", "/api/timeline/sessions/{}/workspace"],
        )
        self.assertEqual(check.missing_paths({"agents.ts": MODULE}, ROUTES), [])

    def test_the_pause_answer_path_that_404d_is_reported(self):
        module = MODULE + """
export function respond(sessionId: string, pauseRequestId: string) {
  return request(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/pause-requests/${pauseRequestId}/response`,
  );
}
"""
        self.assertEqual(
            check.missing_paths({"agents.ts": module}, ROUTES),
            [("agents.ts", "/api/timeline/sessions/{}/pause-requests/{}/response")],
        )

    def test_the_served_pause_answer_path_passes(self):
        module = "request(`/sessions/${sessionId}/pause-requests/${pauseRequestId}/response`)"
        self.assertEqual(check.missing_paths({"agents.ts": module}, ROUTES), [])


if __name__ == "__main__":
    unittest.main()
