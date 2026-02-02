# Evidence-backed refactor ideas (ranked)

Best → worst. Each item includes an evidence script under `ideas/evidence/`.
Run scripts from the repo root.

## Postgres Cleanup (SQLite-only OSS Pivot)

01. [ID 01] Remove agents schema mapping for SQLite-only core.
Evidence: `ideas/evidence/21_evidence_agents_schema_mapping.sh`

02. [ID 02] Drop ensure_agents_schema Postgres-only schema creation.
Evidence: `ideas/evidence/22_evidence_ensure_agents_schema_postgres.sh`

03. [ID 03] Replace postgresql.UUID or JSONB in agents schema migration.
Evidence: `ideas/evidence/23_evidence_alembic_0002_postgres_types.sh`

04. [ID 04] Replace postgresql.UUID in device tokens migration.
Evidence: `ideas/evidence/24_evidence_alembic_0004_postgres_uuid.sh`

05. [ID 05] Replace postgresql.UUID in memories migration.
Evidence: `ideas/evidence/25_evidence_alembic_0007_postgres_uuid.sh`

06. [ID 06] Move Postgres checkpointer to optional module.
Evidence: `ideas/evidence/26_evidence_checkpointer_postgres_path.sh`

07. [ID 07] Remove Postgres advisory lock support from fiche_state_recovery.
Evidence: `ideas/evidence/27_evidence_fiche_state_recovery_advisory.sh`

08. [ID 08] Simplify task_runner Postgres guard logic in SQLite-only mode.
Evidence: `ideas/evidence/28_evidence_task_runner_postgres_guard.sh`

09. [ID 09] Remove asyncpg stub ops_db module that raises NotImplemented.
Evidence: `ideas/evidence/29_evidence_ops_db_asyncpg_stub.sh`

10. [ID 10] Move legacy Postgres test suite out of core repo.
Evidence: `ideas/evidence/30_evidence_tests_readme_legacy_postgres.sh`

11. [ID 11] Remove run_backend_tests.sh legacy Postgres runner.
Evidence: `ideas/evidence/31_evidence_run_backend_tests_postgres.sh`

12. [ID 12] Archive dev-docker Postgres script if Docker is legacy.
Evidence: `ideas/evidence/32_evidence_dev_docker_postgres.sh`

13. [ID 13] Archive stop-docker Postgres script if Docker is legacy.
Evidence: `ideas/evidence/33_evidence_stop_docker_postgres.sh`

14. [ID 14] Move Postgres-only checkpointer tests out of default suite.
Evidence: `ideas/evidence/34_evidence_test_checkpointer_postgres.sh`

15. [ID 15] Remove device-token tests that expect Postgres-only behavior.
Evidence: `ideas/evidence/35_evidence_test_device_tokens_postgres.sh`

16. [ID 16] Remove asyncpg result handling tests once asyncpg removed.
Evidence: `ideas/evidence/36_evidence_test_qa_fiche_asyncpg.sh`

17. [ID 17] Remove advisory-lock support tests after SQLite-only pivot.
Evidence: `ideas/evidence/37_evidence_test_fiche_state_recovery_postgres.sh`

18. [ID 18] Revisit timeseries compatibility tests tied to Postgres assumptions.
Evidence: `ideas/evidence/38_evidence_test_ops_service_timeseries.sh`

## Legacy Tool Registry + Deprecated Code

19. [ID 19] Remove mutable ToolRegistry singleton once tests updated.
Evidence: `ideas/evidence/39_evidence_tool_registry_mutable_singleton.sh`

20. [ID 20] Remove legacy ToolRegistry wiring in builtin tools init.
Evidence: `ideas/evidence/80_evidence_builtin_init_legacy_registry.sh`

21. [ID 21] Drop non-lazy binder compatibility path.
Evidence: `ideas/evidence/40_evidence_lazy_binder_compat.sh`

22. [ID 22] Remove deprecated publish_event_safe wrapper.
Evidence: `ideas/evidence/41_evidence_events_publisher_deprecated.sh`

23. [ID 23] Require envelope-only WS messages, remove legacy wrapping.
Evidence: `ideas/evidence/42_evidence_websocket_legacy_wrap.sh`

24. [ID 24] Remove legacy admin routes without api prefix.
Evidence: `ideas/evidence/43_evidence_admin_legacy_router.sh`

25. [ID 25] Remove deprecated workflow start route.
Evidence: `ideas/evidence/44_evidence_workflow_exec_deprecated_route.sh`

26. [ID 26] Remove deprecated TextChannelController.
Evidence: `ideas/evidence/51_evidence_text_channel_controller_deprecated.sh`

27. [ID 27] Remove deprecated session handler API.
Evidence: `ideas/evidence/52_evidence_session_handler_deprecated.sh`

28. [ID 28] Remove compatibility methods in feedback system.
Evidence: `ideas/evidence/53_evidence_feedback_system_compat.sh`

29. [ID 29] Remove deprecated heuristic or hybrid decision modes in roundabout monitor.
Evidence: `ideas/evidence/54_evidence_roundabout_monitor_deprecated_modes.sh`

30. [ID 30] Remove HEURISTIC or HYBRID decision modes in LLM decider.
Evidence: `ideas/evidence/55_evidence_llm_decider_deprecated_modes.sh`

31. [ID 31] Simplify unified_access legacy behavior.
Evidence: `ideas/evidence/78_evidence_unified_access_legacy.sh`

32. [ID 32] Move or remove legacy ssh_tools from core.
Evidence: `ideas/evidence/77_evidence_ssh_tools_legacy.sh`

33. [ID 33] Update Swarmlet user-agent branding in web_fetch tool.
Evidence: `ideas/evidence/79_evidence_web_fetch_swarmlet_user_agent.sh`

34. [ID 34] Remove legacy workflow trigger upgrade logic in schemas/workflow.py.
Evidence: `ideas/evidence/97_evidence_workflow_schema_legacy_upgrade.sh`

35. [ID 35] Remove deprecated trigger_type field in workflow_schema.py.
Evidence: `ideas/evidence/98_evidence_workflow_schema_deprecated_trigger_type.sh`

36. [ID 36] Tighten trigger_config schema by removing extra allow compatibility.
Evidence: `ideas/evidence/99_evidence_trigger_config_extra_allow.sh`

37. [ID 37] Remove legacy trigger key scanner once legacy shapes dropped.
Evidence: `ideas/evidence/96_evidence_legacy_trigger_check_script.sh`

## Frontend Legacy CSS + Test Signals

38. [ID 38] Remove __APP_READY__ legacy test signal once tests updated.
Evidence: `ideas/evidence/45_evidence_app_ready_legacy_signal.sh`

39. [ID 39] Drop legacy React Flow selectors in CSS after test update.
Evidence: `ideas/evidence/46_evidence_canvas_react_legacy_selectors.sh`

40. [ID 40] Remove legacy buttons.css compatibility layer.
Evidence: `ideas/evidence/47_evidence_buttons_css_legacy.sh`

41. [ID 41] Remove legacy modal pattern CSS.
Evidence: `ideas/evidence/48_evidence_modal_css_legacy.sh`

42. [ID 42] Remove legacy util margin helpers once migrated.
Evidence: `ideas/evidence/49_evidence_util_css_legacy.sh`

43. [ID 43] Remove legacy token aliases after CSS migration.
Evidence: `ideas/evidence/50_evidence_tokens_css_legacy_aliases.sh`
