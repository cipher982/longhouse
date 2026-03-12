# Longhouse Tool Surface Tightening

Status: In progress
Owner: Codex
Last updated: 2026-03-12

## Executive Summary

Longhouse currently exposes too much product surface in too many places:

- normal local terminal agents can end up with Longhouse MCP tools just because session sync was installed
- cloud commis/resume workspaces correctly get Longhouse context tools injected for bounded execution
- Oikos has its own internal tool surface, but docs and product language blur that with Longhouse MCP

For OSS launch and demos, the default product surface must be legible and easy to explain. This cleanup tightens Longhouse around the actual product story:

1. session continuity and live shipping
2. cloud resume from any device
3. cross-session intelligence via search/recall
4. Oikos as the assistant layer that can inspect, continue, or escalate

Anything outside that boundary should be removed from defaults or moved out of the Longhouse MCP surface.

## Problem Statement

Today, a user can install session shipping with `longhouse connect --install`, and Longhouse will also auto-register a global MCP server in the user's normal local Claude/Codex configs. That means a feature intended for Longhouse continuity quietly changes the tool menus of ordinary local terminal agents.

At the same time, the Longhouse MCP server includes tools that do not cleanly fit the launch story:

- local KV `memory_read` / `memory_write` backed by `~/.claude/longhouse-memory.json`
- `get_reflections`
- `visual_compare`

These are individually interesting, but they increase cognitive load and create product ambiguity. The owner should be able to explain every default capability without surprise.

## Scope

In scope:

- Longhouse MCP server tool list
- `longhouse connect --install` and `--hooks-only` default behavior
- workspace-scoped MCP injection for cloud commis/resume sessions
- docs/help text/AGENTS/VISION/README alignment
- tests for the new boundary
- deploy + hosted verification

Out of scope:

- removing Oikos internal orchestration/runtime features
- redesigning the builtin Oikos/commis tool registries beyond necessary doc alignment
- canonical memory-system consolidation inside builtin tools
- removing `longhouse mcp-server` itself

## Product Boundary

### Surface A: Normal local terminal

What users think they are doing:

- installing sync/hooks for local sessions
- continuing to use Claude/Codex normally in their own terminal

What Longhouse should do here:

- ship session data
- install hook integration needed for shipping/presence
- avoid silently changing the user's everyday tool surface

### Surface B: Cloud commis / resume workspaces

What users think they are doing:

- resuming a Longhouse-managed session in the cloud
- letting a Longhouse-managed coding agent work with shared context

What Longhouse should do here:

- inject the Longhouse MCP server into the workspace-local config
- expose the minimal set of continuity/search/Oikos callback tools

### Surface C: Oikos internal runtime

What users think they are doing:

- interacting with the assistant layer

What Longhouse should do here:

- keep Oikos/commis builtin tool allowlists separate from the public/default Longhouse MCP story
- document Oikos as an assistant/control layer, not as proof that every internal tool belongs in the default local MCP surface

## Decision Log

### Decision: Remove global MCP auto-registration from `connect --install`
Context: Session shipping install should not change normal local terminal tool menus.
Choice: `longhouse connect --install` and `--hooks-only` will stop writing Longhouse MCP entries into `~/.claude.json` and `~/.codex/config.toml`.
Rationale: Sync install and local MCP opt-in are different jobs. The existing behavior creates surprise and makes Longhouse feel bloated.
Revisit if: Longhouse later grows a deliberate, separate "install local assistant tools" command with a clear user-facing story.

### Decision: Keep workspace-scoped MCP injection for cloud commis/resume
Context: Cloud commis/resume sessions are core Longhouse product surfaces and need shared context.
Choice: Keep writing Longhouse MCP config into workspace-local `.claude/settings.json` / `.codex/config.toml` for provisioned workspaces.
Rationale: This is the clean place for Longhouse context tools to exist.
Revisit if: Commis/resume runtime moves away from CLI MCP integration entirely.

### Decision: Trim Longhouse MCP to continuity/Oikos-fit tools
Context: The default Longhouse MCP server currently mixes continuity features with generic utilities and a misleading local-only memory primitive.
Choice: Keep only `search_sessions`, `get_session_detail`, `get_session_events`, `recall`, `log_insight`, `query_insights`, and `notify_oikos`.
Rationale: These tools directly serve continuity, cross-session intelligence, or Oikos assistance.
Revisit if: Additional tools become central to the launch story and can be explained in one sentence.

### Decision: Remove Longhouse MCP local KV memory
Context: `memory_read` / `memory_write` in the Longhouse MCP server operate on a local JSON file, not shared Longhouse memory.
Choice: Remove these tools from the Longhouse MCP server and stop documenting them as Longhouse memory.
Rationale: The feature name promises shared product value, but the implementation is a local-agent convenience. That is misleading and overlaps conceptually with other memory systems.
Revisit if: Longhouse grows one canonical shared memory product and can expose it under a single, clearly defined interface.

### Decision: Remove `get_reflections` and `visual_compare` from Longhouse MCP
Context: Both features may be useful internally, but neither is part of the continuity/cloud-resume story.
Choice: Remove both from the Longhouse MCP server and default docs.
Rationale: Lean toward removal unless a tool has strong product fit and obvious launch value.
Revisit if: Reflection briefings or visual QA become explicit user-facing product features.

## Target Behavior

### `longhouse connect --install`

Must:

- install/refresh the shipper service
- install/refresh Claude hooks
- persist URL/token/machine-name as before

Must not:

- write `mcpServers.longhouse` into the user's global Claude config
- write `[mcp_servers.longhouse]` into the user's global Codex config

### Cloud workspace provisioning

Must:

- continue injecting workspace-local Longhouse MCP config for Claude and Codex workspaces
- continue allowing bounded cloud commis/resume sessions to use Longhouse continuity tools

### Longhouse MCP server

Must expose:

- `search_sessions`
- `get_session_detail`
- `get_session_events`
- `recall`
- `log_insight`
- `query_insights`
- `notify_oikos`

Must not expose:

- `memory_read`
- `memory_write`
- `get_reflections`
- `visual_compare`

## Acceptance Criteria

1. `longhouse connect --install` no longer performs global MCP registration for Claude or Codex.
2. `longhouse connect --hooks-only` no longer performs global MCP registration for Claude or Codex.
3. Workspace provisioning still injects Longhouse MCP config into workspace-local Claude/Codex config files.
4. The Longhouse MCP server tool list exactly matches the kept-tool set in this spec.
5. README, AGENTS, VISION, and CLI/help text no longer describe global MCP auto-install as part of shipping install.
6. README, AGENTS, and VISION no longer describe Longhouse MCP local KV memory, reflections, or visual compare as part of the default Longhouse tool surface.
7. Automated tests cover:
   - connect install/hook flows not calling global MCP registration
   - workspace injection still writing Longhouse MCP config
   - Longhouse MCP server exposing the expected trimmed tool list
8. Local verification passes.
9. Hosted deploy is completed and the primary dev instance remains healthy after reprovision.

## Implementation Phases

### Phase 0: Spec and task tracking

Deliverables:

- `TODO.md` entry
- this spec
- task checklist

Acceptance:

- artifacts committed

### Phase 1: Trim Longhouse MCP server

Deliverables:

- remove out-of-scope MCP tools from `mcp_server/server.py`
- remove now-unused helpers/client methods as needed
- add/adjust tests for MCP tool inventory

Acceptance:

- Longhouse MCP server exposes only the kept tools

### Phase 2: Remove global MCP auto-registration from local install

Deliverables:

- stop `connect --install` and `--hooks-only` from registering global MCP entries
- remove or retire unused global-install helpers if they no longer serve a purpose
- keep workspace injection intact
- add tests covering the new behavior

Acceptance:

- local install behavior is shipper-only by default
- workspace injection still works

### Phase 3: Docs, generated artifacts, deploy, verify

Deliverables:

- update README, AGENTS, VISION, and any CLI/help text
- regenerate artifacts if needed
- run local test suite slices + E2E/qa checks appropriate to touched surfaces
- push, wait for CI/build, deploy control plane, reprovision dev instance, verify prod

Acceptance:

- docs match reality
- local tests pass
- hosted verification passes

## Test Plan

Local:

- targeted backend unit tests for shipper/connect/workspace/MCP server changes
- `make test`
- relevant frontend/type generation checks if touched

Hosted / deploy:

- push to `main`
- wait for GH runtime image/deploy workflows
- verify marketing/control plane deployment status
- reprovision `david010` instance
- run `make qa-live`
- check `https://david010.longhouse.ai/api/health`

## Risks

- Hidden docs/help text may still imply global MCP install
- Removing tools from the MCP server may break untested internal expectations
- Deploy verification can fail for unrelated hosted drift, so logs and health checks must be reviewed before concluding regression
