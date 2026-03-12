# Longhouse Tool Surface Tightening Tasks

Status: In progress
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
- [ ] Commit Phase 1

## Phase 2: Local install boundary

- [ ] Stop `connect --hooks-only` from globally registering Longhouse MCP
- [ ] Stop `connect --install` from globally registering Longhouse MCP
- [ ] Remove or retire unused global MCP registration helpers if dead
- [ ] Keep workspace-local Claude MCP injection working
- [ ] Keep workspace-local Codex MCP injection working
- [ ] Add/adjust tests for local install vs workspace injection
- [ ] Commit Phase 2

## Phase 3: Docs and verification

- [ ] Update README command/help text
- [ ] Update `AGENTS.md`
- [ ] Update `VISION.md`
- [ ] Update any other stale references discovered during implementation
- [ ] Regenerate artifacts if required
- [ ] Run targeted tests
- [ ] Run broader local verification (`make test` and any necessary follow-on checks)
- [ ] Commit Phase 3

## Phase 4: Ship and verify

- [ ] Push `main`
- [ ] Wait for CI/build workflows to finish successfully
- [ ] Deploy hosted control plane/marketing if needed
- [ ] Reprovision `david010` user instance
- [ ] Verify hosted health and `make qa-live`
- [ ] Update this task doc with final status notes
- [ ] Commit any last doc/status updates if needed
