# Stream Replay/Live Router Refactor

Status: Done
Spec: `docs/specs/run-stream-refactor.md`
Last updated: 2026-03-17

## Goal

Keep `stream.py` as thin router glue by moving replay/live lifecycle state into smaller, testable units without changing behavior.

## Checklist

- [x] Write the extraction plan and guardrail test matrix
- [x] Extract replay/lifecycle/continuation/subscription logic into `services/run_stream.py`
- [x] Add focused regression coverage for continuation aliasing, stream-control, overflow, and completion behavior
- [x] Re-run clean-tree repo verification and close the task

## Notes

- 2026-03-14: The code refactor landed in small slices; the only remaining blocker was unrelated dirty-tree runner work during the original closeout pass.
- 2026-03-17: `make test` passed on a clean tree (`879` backend lite, `130` control-plane, engine `114 + 6 + 3`), so the lingering status drift is resolved and the task is closed.
