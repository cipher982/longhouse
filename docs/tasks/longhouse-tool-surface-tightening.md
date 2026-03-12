# Longhouse Tool Surface Tightening Tasks

Status: Done
Spec: `docs/specs/longhouse-tool-surface-tightening.md`
Last updated: 2026-03-12

## Phase 0: Spec

- [x] Add `TODO.md` tracking entry
- [x] Write spec with explicit keep/remove decisions
- [x] Write granular task checklist
- [x] Commit Phase 0 artifacts

## Phase 1: MCP server cleanup

- [x] Remove Longhouse MCP local KV memory tools
- [x] Remove `get_reflections` from Longhouse MCP
- [x] Remove `visual_compare` from Longhouse MCP
- [x] Remove any now-unused MCP client/helper code
- [x] Add/adjust tests for the trimmed Longhouse MCP tool list
- [x] Commit Phase 1

## Phase 2: Local install boundary

- [x] Stop `connect --hooks-only` from globally registering Longhouse MCP
- [x] Stop `connect --install` from globally registering Longhouse MCP
- [x] Remove or retire unused global MCP registration helpers if dead
- [x] Keep workspace-local Claude MCP injection working
- [x] Keep workspace-local Codex MCP injection working
- [x] Add/adjust tests for local install vs workspace injection
- [x] Commit Phase 2

## Phase 3: Docs and verification

- [x] Update README command/help text
- [x] Update `AGENTS.md`
- [x] Update `VISION.md`
- [x] Update any other stale references discovered during implementation
- [x] Regenerate artifacts if required
- [x] Run targeted tests
- [x] Run broader local verification (`make test` and any necessary follow-on checks)
- [x] Commit Phase 3

Notes:
- 2026-03-12: No generated OpenAPI/tool-schema artifacts were required for this cleanup. The public API surface did not change; only Longhouse MCP defaults, install behavior, and docs changed.

## Phase 4: Ship and verify

- [x] Push `main`
- [x] Wait for CI/build workflows to finish successfully
- [x] Deploy hosted control plane/marketing if needed
- [x] Reprovision `david010` user instance
- [x] Verify hosted health and `make qa-live`
- [x] Update this task doc with final status notes
- [x] Commit any last doc/status updates if needed

Final notes:
- Phase commits: `bc661142` (spec), `e6c8296e` (trim MCP), `ba7e61f1` (split local install from workspace MCP), `d5c14346` (docs), `dd4a3537` (runner fixture drift), `3d401607` (dashboard accessibility).
- Local verification passed with targeted pytest, `make test`, `make test-e2e`, `make test-runner-unit`, `make test-readmes MODE=smoke`, and frontend type validation.
- Hosted verification passed with healthy `/api/health` and `make qa-live` (`8 passed`).
- Final GitHub Actions runs for commit `3d401607ddef95f918be734752ef8be970233633` completed successfully, including `push-pr-ci` run `23023163344` and `Deploy and Verify` run `23023226471`.
