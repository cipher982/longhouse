# Skills Platform v1 - Goals, UX, Integration, and Testable Finish Conditions

## Summary
Skills are markdown-based instruction bundles with YAML frontmatter. Tools remain the
canonical execution surface. Skills are layered on top for guidance, eligibility
checks, and optional tool dispatch. This spec defines the UX for users to add
custom skills, the internal wiring to prompts/tools, and testable finish
conditions including prod E2E verification.

## Goals
- Make skills a first-class, testable layer without replacing tools.
- Improve model behavior by injecting skill guidance into supervisor/worker
  system prompts.
- Provide a user-facing path to add and manage custom skills for their agents.
- Allow skills to safely wrap tools via tool_dispatch with eligibility gating.
- Ensure end-to-end behavior is verifiable via automated tests and prod E2E.

## Non-Goals
- Replacing tool schemas or MCP integration with skills.
- Allowing users to edit core system prompts or global policies.
- Auto-installing external binaries or setting secrets on behalf of the user.

## Definitions
- Tool: Executable function with a schema, registered in the tool registry.
- MCP Tool: External tool exposed via MCP and adapted into the registry.
- Skill: SKILL.md file with YAML frontmatter + instructional content.
- Skill Dispatch: Optional mapping from a skill to a tool name.
- Eligibility: Skill readiness based on env/binary/config requirements.

## Current State (as of 2026-01-26)
- Skills loader, parser, registry, and integration helpers exist under
  `apps/zerg/backend/zerg/skills/`.
- Bundled skills exist: github, slack, web-search.
- Skills API exists: `/api/skills`, `/api/skills/commands`, `/api/skills/prompt`.
- Skills are NOT wired into supervisor/worker prompts or tool registry.
- Frontend does not currently call the skills API.

## Design Principles
- Tools remain the only execution surface.
- Skills are additive: guidance + UX + eligibility + optional dispatch.
- Skills never expand privileges beyond tool allowlists.
- User skills are opt-in per agent or per workspace.

## UX and User Flows

### Onboarding: Add a Skill
1. User opens Skills Library.
2. User chooses scope:
   - Personal (user-wide)
   - Workspace (per project/repo)
3. User uploads or creates a SKILL.md.
4. System parses frontmatter, validates name, and displays:
   - Description, emoji
   - Requirements (env, binaries, config)
   - Eligibility status (eligible/ineligible + missing items)
5. User confirms and saves.

### Managing Skills
- Skills Library lists bundled, user, and workspace skills.
- Filters: scope, eligibility, invocability (user/model), source.
- Skill detail view shows full content, requirements, and a test button.

### Using Skills
- User-invocable skills appear as slash commands.
- Model-invocable skills appear in system prompt guidance.
- If a skill has tool_dispatch, a wrapper tool is available (skill_<name>).

### Eligibility UX
- Eligible: green badge, ready to use.
- Ineligible: warning badge, show missing items and CTA
  ("Connect Slack", "Set GITHUB_TOKEN", "Install gh").

## Internal Integration

### Skill Loading and Scope
- Bundled: `apps/zerg/backend/zerg/skills/bundled/*/SKILL.md`
- User (DB-backed): stored in database, materialized in-memory for parsing
- Workspace: `<workspace>/skills/*/SKILL.md`
- Precedence: package < bundled < user (DB) < workspace

### Prompt Injection
- Inject skill prompt into supervisor and worker system prompts.
- Default insertion: end of system prompt (append).
- Filter to model_invocable skills only.
- Default to compact metadata (name + description + tool dispatch); load full SKILL.md only when needed.
- Allow allowlist patterns per agent or per run.

### Tool Dispatch Integration
- If skill.manifest.tool_dispatch is set, create a wrapper tool
  named `skill_<skill_name>`.
- Wrapper tool must call an existing tool by name (builtin or MCP).
- Wrapper tool must not bypass tool allowlists.
- Wrapper tool description should include skill name + summary.

### Allowlist Rules
- Skills are filtered by eligibility first.
- User skills are opt-in. A user must explicitly allow skill names
  for a given agent or workspace.
- Bundled skills may be enabled by default for core agents.

### Skill Settings (Agent Config / User Context)
- `skills_enabled`: bool (default true)
- `skills_allowlist`: list or comma-separated string of patterns
- `skills_include_user`: bool (default false)
- `skills_max`: int (optional cap for prompt injection)

### Observability
- Add structured log or run event when a skill prompt is injected.
- Add structured log when a skill wrapper tool is called
  (skill name, target tool, run_id).

## API Requirements

### Existing
- GET /api/skills
- GET /api/skills/commands
- GET /api/skills/prompt
- GET /api/skills/{skill_name}
- POST /api/skills/reload

### New (User Skills CRUD)
- POST /api/skills (create skill, scope, content)
- PATCH /api/skills/{skill_name} (update content/metadata)
- DELETE /api/skills/{skill_name}

Notes:
- User skills are DB-backed (no filesystem writes).
- Workspace skills remain filesystem-based and must be path-validated.

## Data and Storage
- Canonical format: SKILL.md with YAML frontmatter (stored as text in DB for user skills).
- Bundled skills are versioned in repo.
- User skills are stored in DB as the source of truth.
- Workspace skills remain in workspace directory.

## Security
- Validate skill names (alphanumeric, hyphen, underscore).
- Sanitize or reject invalid YAML frontmatter.
- Tool dispatch must be checked against allowed tools.
- User skills must not modify or override core system prompts.

## Testing Strategy

### Unit Tests
- Parser and loader: frontmatter parsing, validation, precedence.
- Registry: allowlist filtering, prompt formatting.
- Integration: tool_dispatch wrapper creation.

### Integration Tests
- Prompt injection: supervisor/worker prompts include skills block.
- Tool allowlist: skill wrapper tools only available when allowed.

### E2E Tests (Local)
- Add a workspace skill and verify:
  - It appears in /api/skills
  - It appears in /api/skills/commands if user_invocable
  - It appears in /api/skills/prompt if model_invocable
- Trigger a chat that uses the skill wrapper tool and verify result
  in run artifacts.

### Prod E2E Verification
- Extend `make verify-prod` to include:
  - List skills endpoint (expected bundled skill count >= 3)
  - Skill prompt injection check (non-empty prompt includes "# Available Skills")
  - Tool dispatch smoke test (call a skill wrapper tool in a safe sandbox)

## Finish Conditions (Testable)

FC-1: Skills list returns bundled + user + workspace skills with eligibility.
- Test: `GET /api/skills` in unit/integration + prod verify.

FC-2: Skills prompt injection works for supervisor and worker.
- Test: integration test asserts system prompt includes skills block.

FC-3: Only model_invocable skills appear in injected prompt.
- Test: unit test for registry.format_skills_prompt.

FC-4: User-invocable skills appear in /api/skills/commands.
- Test: integration test with a workspace skill.

FC-5: Skill wrapper tools are registered when tool_dispatch is set.
- Test: unit test + runtime check in tool registry.

FC-6: Skill wrapper tools respect allowlists and do not bypass permissions.
- Test: integration test for allowlist filtering.

FC-7: Onboarding UX allows adding a skill and seeing eligibility state.
- Test: Playwright E2E for Skills Library flow.

FC-8: User can invoke a skill via slash command in Jarvis.
- Test: Playwright E2E triggers /command and verifies run uses skill.

FC-9: Prod verification includes skills health checks and passes.
- Test: `make verify-prod` includes skills checks and succeeds.

## Rollout
1. Internal wiring (prompt injection + tool dispatch).
2. Skills Library UI read-only (list + detail).
3. Skills CRUD for user/workspace skills.
4. Slash commands + onboarding UX.
5. Tighten allowlists and prod E2E checks.

## Open Questions
- Should tool_dispatch allow wildcards or only exact tool names?
- Do we need per-agent skill allowlists or per-workspace only?
