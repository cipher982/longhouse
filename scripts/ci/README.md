# CI Test Runner

## Spec (short)
- Goal: trigger a single, explicit CI suite for a specific ref and show concise status.
- Interface: `scripts/ci/run-on-ci.sh <suite> [ref] [--test <path>] [--no-watch]`.
- Safety: workflow only runs allowlisted suites; e2e-single validates `--test` path.
- Output: prints run URL + status transitions; no log spam by default.

## Suites
- validate, unit, frontend, runner-unit, e2e-core, e2e-a11y, e2e-single, full

## Notes
- Use `WORKFLOW_REF=<branch>` when testing workflow changes on non-main.
- Optional future: MCP helper to dispatch and stream CI results.
