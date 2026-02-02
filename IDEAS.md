# Simplification / Refactor Ideas (100)

1. Rename OSS default email `local@zerg` to `local@longhouse` in `services/single_tenant.py` to match branding.
2. Standardize env vars and file names from `ZERG_*`/`zerg-*` to `LONGHOUSE_*`/`longhouse-*` (e.g., `session_continuity.py`, `shipper/token.py`) and remove fallback names after migration.
3. Delete Life Hub compatibility functions (`fetch_session_from_life_hub`, `ship_session_to_life_hub`) in `services/session_continuity.py`.
4. Remove Life Hub references in tools/docs (`oikos_tools.py` resume_session_id docs, `docs/session-resume-design.md`, Sauron life-hub config).
5. Collapse "Forum" vs "Sessions" vs "Timeline" naming to a single concept; rename route/components accordingly (`ForumPage`, `SessionsPage`).
6. Make `/timeline` the default authenticated landing route and drop dashboard redirect logic in `routes/App.tsx`.
7. Remove `DashboardPage` if it doesn't add unique value beyond timeline (align with VISION).
8. Reduce public routes to only those needed for Longhouse (pricing/docs/etc); remove unused pages like `ReliabilityPage` if not surfaced.
9. Move multi-tenant scaffolding (workspace columns, owner_id everywhere) into a separate optional module or remove if single-tenant is enforced (e.g., `models/models.py` workflows).
10. Remove `ADMIN_EMAILS` fallback in `get_owner_email()` once OWNER_EMAIL is the single source of truth.
11. Split `apps/zerg/backend/zerg/main.py` into app factory + router wiring + startup tasks modules.
12. Break `routers/agents.py` into `agents_auth.py`, `agents_ingest.py`, `agents_query.py`, `agents_export.py`.
13. Move in-memory rate limiting in `routers/agents.py` to middleware or a small service; keep router logic thin.
14. Remove legacy `AGENTS_API_TOKEN` fallback and require device tokens only in production.
15. Extract shared auth logic (cookie + token) into `dependencies/agents_auth.py` to avoid duplicative checks.
16. Promote `build_demo_agent_sessions` usage to CLI-only (seed command), remove from public API endpoints.
17. Consolidate repeated request parsing/validation in routers into shared Pydantic models.
18. Remove `_mount_legacy` admin router and the `/admin/reset-database` legacy endpoint in `routers/admin.py`.
19. Split `routers/oikos.py` into subrouters for chat, runs, and commis operations.
20. Split `routers/runners.py` into registration, connection management, and job dispatch modules.
21. Drop legacy envelope auto-wrapping in `websocket/handlers.py` and require envelope-only messages.
22. Remove deprecated `subscribe_thread` WS semantics and related compatibility code paths.
23. Remove `publish_event_safe` and other legacy wrappers in `events/publisher.py`.
24. Consolidate WS message/handlers with SSE event models to reduce duplicate schema definitions.
25. Finish splitting `models/models.py` into domain files; stop re-exporting everything from a single mega-module.
26. Remove workflow/canvas models from core if not central to Longhouse vision; keep them in optional feature package.
27. Simplify JSON usage: replace `MutableDict`/`MutableList` where full row rewrites are ok.
28. Remove Postgres-only partial indexes (`postgresql_where`) when SQLite is the default.
29. Drop Postgres-specific UUID/JSONB variants from migrations and use plain JSON/UUID.
30. Reduce `models_config` indirection by moving defaults into `config.Settings`.
31. Simplify `services/checkpointer.py` to SQLite-only; move Postgres saver to optional dependency module.
32. Remove advisory-lock checks in `fiche_state_recovery.py` and `task_runner.py` if Postgres support is optional.
33. Retire legacy Postgres-heavy test suite (`apps/zerg/backend/tests`) to a separate repo; keep lite suite in-tree.
34. Remove `jobs/ops_db.py` asyncpg stub and any codepaths that raise NotImplemented for SQLite.
35. Drop Postgres-specific code in tests (asyncpg handling, postgres-only device token tests) once SQLite is canonical.
36. Split `services/oikos_react_engine.py` into state machine, tool execution, and message assembly modules.
37. Split `services/oikos_service.py` into API adapter vs run management vs persistence layers.
38. Split `services/commis_resume.py` into barrier coordination, inbox follow-up, and DB message helpers.
39. Merge `commis_job_queue.py`, `commis_job_processor.py`, and `commis_runner.py` into a single `commis` package with clear interfaces.
40. Remove deprecated execution modes (`local`, `cloud`) in `spawn_commis_async` and accept only `standard`/`workspace`.
41. Delete ignored internal params like `_skip_interrupt` in `oikos_tools.py` to reduce API surface.
42. Move commis artifact storage + tool output storage into one artifact subsystem (single index).
43. Replace polling-based roundabout with event-driven completion where possible; keep LLM decision as optional add-on.
44. Remove heuristic and hybrid modes from `llm_decider.py` and `roundabout_monitor.py`.
45. If roundabout remains, move config constants into `config.Settings` for centralized tuning.
46. Remove `openai_realtime` service and old realtime paths if no longer used.
47. Remove deprecated `TextChannelController` in frontend and any callers.
48. Remove `session-handler` legacy methods and enforce SSOT paths only.
49. Remove `libs/agent_runner` wrapper and update imports to `hatch` directly.
50. Consolidate `oikos_context` + run context logic into a single context object.
51. Move shipper logic out of backend `services/shipper/` into CLI package; backend should only expose ingest.
52. Centralize Claude config dir resolution (used in `token.py`, `session_continuity.py`, shipper) into a shared helper.
53. Remove legacy token/url file names (`zerg-device-token`, `zerg-url`) once migration is done.
54. Rename `ZERG_API_URL` -> `LONGHOUSE_API_URL` and drop old env var fallback.
55. Extract session path encoding/validation into a pure utility module for reuse and testability.
56. Split `tools/builtin/oikos_tools.py` (1.5k lines) into commis tools, session tools, and admin tools.
57. Remove `ToolRegistry` mutable singleton and the `register_tool` decorator once tests are updated.
58. Move connector tools (jira, linear, slack, notion, github) into optional plugin packages to keep core slim.
59. Remove `ssh_tools` if runner-based execution is the standard path; keep as plugin if needed.
60. Consolidate `memory_tools`, `oikos_memory_tools`, and `fiche_memory_tools` into one memory API.
61. Collapse `tool_discovery.py` and `tool_search.py` into one tool index module (avoid duplicate search logic).
62. Remove `personal_tools.py` from OSS core (user-specific tooling).
63. Move `zerg/jobs` to a separate jobs-pack repo (align with VISION’s jobs pack concept).
64. Delete `jobs/life_hub/` and any Life Hub job imports from `jobs/registry.py`.
65. Remove `jobs/examples/` or move to docs; keep runtime package minimal.
66. Simplify `jobs/registry.py` to load from a single configurable jobs source; remove hardcoded imports.
67. Remove `jobs/git_sync.py` if jobs are bundled locally for OSS by default.
68. Consolidate `scheduler_service.py`, `workflow_scheduler.py`, and `task_runner.py` into a single scheduler path.
69. If Sauron is the scheduler, strip scheduler logic from core backend and keep only a thin client.
70. Split `config.Settings` into nested dataclasses (auth, db, agents, runners, shipper) for readability.
71. Replace `app_public_url`, `public_site_url`, `public_api_url` with one canonical base URL.
72. Move `oikos_workspace_path` default to `~/.longhouse/workspaces` (align with VISION/AGENTS gotcha).
73. Remove unused provider keys (`groq_api_key`, etc.) if they’re not supported in current product.
74. Replace repeated `os.getenv` usage with a small `getenv_*` helper for consistent parsing.
75. Split `SessionsPage.tsx` into smaller components (filters, cards, grouping) and move helpers into `lib/date.ts`.
76. Merge `ForumPage` + `SessionsPage` into a single "Timeline" page.
77. Remove `/forum` route if timeline is the canonical session UI.
78. Break up large pages (`AdminPage.tsx`, `CanvasPage.tsx`, `SwarmOpsPage.tsx`) into subpages or lazy sections.
79. Delete styles legacy alias variables in `tokens.css` and remove `styles/css/buttons.css` compatibility layer.
80. Remove E2E compatibility CSS classes for React Flow if tests updated to new selectors.
81. Replace `OikosChatController` SSE parsing with shared generated helpers to reduce custom logic.
82. Consolidate Oikos state managers (`stateManager`, `conversationController`, `commisProgressStore`) into one store.
83. Remove unused `components/icons.tsx` mega-file by splitting into per-feature icons or using inline SVG imports.
84. Remove `openapi-types.ts` bloat from default bundles by splitting per-feature type modules or lazy importing.
85. Rename UI labels and routes from "Zerg", "Swarm", "Forum" to "Longhouse", "Runs", "Timeline".
86. Move legacy test compatibility logic out of CSS/JS (e.g., `canvas-react.css` legacy selectors) once tests updated.
87. Remove deprecated CLI flags in scripts (`smoke-prod.sh --chat` etc.) to simplify help output.
88. Archive or delete Docker helper scripts (`scripts/dev-docker.sh`, `scripts/stop-docker.sh`) if Docker is now legacy.
89. Update docs to remove Life Hub references (`docs/session-resume-design.md`) and align with VISION's ingest/export.
90. Convert demo session data to a seeded SQLite file rather than code-driven builders (`services/demo_sessions.py`).
91. Remove redundant JSON helper functions in `utils/json_helpers.py` now that MutableDict is standard.
92. Consolidate compatibility helpers (`core/config.py`, `constants.py`) into a single `compat.py` or delete if unused.
93. Remove deprecated wrappers in `prompts/__init__.py` and prompt shims like `oikos_prompt.py` if new composer is canonical.
94. Delete unused `email/providers.py` legacy entrypoint if connectors are the only path.
95. Drop legacy auth re-exports (e.g., `JWT_SECRET`, `DEV_EMAIL`) once the new auth strategy is settled.
96. Remove `run_backend_tests.sh` if only lite suite is supported; keep one test entrypoint.
97. Reduce log / telemetry scaffolding (`e2e_logging_hacks.py`) into a dev-only module.
98. Rename remaining `zerg` config dir paths or data dirs to `~/.longhouse` as per VISION.
99. Consolidate `events` and `callbacks` modules if they overlap in purpose (event emission vs token stream).
100. Create a clear top-level `core/agents` package that contains sessions/events ingest/query/export and move everything else under `features/` to highlight the product center.
