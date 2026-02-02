# Evidence-backed refactor ideas

Each item includes an evidence script under `ideas/evidence/`.
Run scripts from the repo root.

01. Make /timeline the default authenticated landing per VISION (root route currently landing or dashboard logic).
Evidence: `ideas/evidence/01_evidence_timeline_default_route_mismatch.sh`

02. Remove or merge DashboardPage since VISION says timeline is primary user landing.
Evidence: `ideas/evidence/02_evidence_dashboard_page_exists.sh`

03. Consolidate ForumPage and SessionsPage into single Timeline experience.
Evidence: `ideas/evidence/03_evidence_forum_sessions_dupe_pages.sh`

04. Collapse standalone forum subsystem into timeline; forum folder still exists.
Evidence: `ideas/evidence/04_evidence_forum_folder_subsystem.sh`

05. Rename SwarmOpsPage to Runs or remove Swarm naming not in VISION.
Evidence: `ideas/evidence/05_evidence_swarmops_naming.sh`

06. Replace api.zerg.ai references with longhouse.ai branding.
Evidence: `ideas/evidence/06_evidence_api_zerg_ai_url.sh`

07. Rename ZERG_API_URL env var to LONGHOUSE_API_URL and drop fallback.
Evidence: `ideas/evidence/07_evidence_zerg_api_url_env.sh`

08. Remove ~/.zerg migration path once longhouse is canonical.
Evidence: `ideas/evidence/08_evidence_cli_migrate_dot_zerg.sh`

09. Update skills loader to use ~/.longhouse only (no ~/.zerg fallback).
Evidence: `ideas/evidence/09_evidence_skills_loader_dot_zerg.sh`

10. Drop legacy token filename zerg-device-token after migration.
Evidence: `ideas/evidence/10_evidence_shipper_legacy_token_filename.sh`

11. Drop legacy url filename zerg-url after migration.
Evidence: `ideas/evidence/11_evidence_shipper_legacy_url_filename.sh`

12. Rename default runner image from zerg-runner to longhouse-runner.
Evidence: `ideas/evidence/12_evidence_runner_image_branding.sh`

13. Update session resume doc to remove Life Hub flow.
Evidence: `ideas/evidence/13_evidence_life_hub_doc_resume.sh`

14. Update tool docstrings referencing Life Hub session resume.
Evidence: `ideas/evidence/14_evidence_life_hub_in_oikos_tools.sh`

15. Remove fetch or ship life hub aliases in session continuity.
Evidence: `ideas/evidence/15_evidence_life_hub_aliases_in_session_continuity.sh`

16. Move or remove jobs/life_hub from core repo.
Evidence: `ideas/evidence/16_evidence_life_hub_jobs_folder.sh`

17. Remove life_hub imports in jobs registry.
Evidence: `ideas/evidence/17_evidence_life_hub_jobs_registry_import.sh`

18. Strip Life Hub networks or env from sauron docker-compose.
Evidence: `ideas/evidence/18_evidence_sauron_docker_compose_life_hub.sh`

19. Update sauron README to remove Life Hub dependencies.
Evidence: `ideas/evidence/19_evidence_sauron_readme_life_hub.sh`

20. Update e2e hatch script referencing ship_session_to_life_hub.
Evidence: `ideas/evidence/20_evidence_e2e_hatch_life_hub.sh`

21. Remove agents schema mapping for SQLite-only core.
Evidence: `ideas/evidence/21_evidence_agents_schema_mapping.sh`

22. Drop ensure_agents_schema Postgres-only schema creation.
Evidence: `ideas/evidence/22_evidence_ensure_agents_schema_postgres.sh`

23. Replace postgresql.UUID or JSONB in agents schema migration.
Evidence: `ideas/evidence/23_evidence_alembic_0002_postgres_types.sh`

24. Replace postgresql.UUID in device tokens migration.
Evidence: `ideas/evidence/24_evidence_alembic_0004_postgres_uuid.sh`

25. Replace postgresql.UUID in memories migration.
Evidence: `ideas/evidence/25_evidence_alembic_0007_postgres_uuid.sh`

26. Move Postgres checkpointer to optional module.
Evidence: `ideas/evidence/26_evidence_checkpointer_postgres_path.sh`

27. Remove Postgres advisory lock support from fiche_state_recovery.
Evidence: `ideas/evidence/27_evidence_fiche_state_recovery_advisory.sh`

28. Simplify task_runner Postgres guard logic in SQLite-only mode.
Evidence: `ideas/evidence/28_evidence_task_runner_postgres_guard.sh`

29. Remove asyncpg stub ops_db module that raises NotImplemented.
Evidence: `ideas/evidence/29_evidence_ops_db_asyncpg_stub.sh`

30. Move legacy Postgres test suite out of core repo.
Evidence: `ideas/evidence/30_evidence_tests_readme_legacy_postgres.sh`

31. Remove run_backend_tests.sh legacy Postgres runner.
Evidence: `ideas/evidence/31_evidence_run_backend_tests_postgres.sh`

32. Archive dev-docker Postgres script if Docker is legacy.
Evidence: `ideas/evidence/32_evidence_dev_docker_postgres.sh`

33. Archive stop-docker Postgres script if Docker is legacy.
Evidence: `ideas/evidence/33_evidence_stop_docker_postgres.sh`

34. Move Postgres-only checkpointer tests out of default suite.
Evidence: `ideas/evidence/34_evidence_test_checkpointer_postgres.sh`

35. Remove device-token tests that expect Postgres-only behavior.
Evidence: `ideas/evidence/35_evidence_test_device_tokens_postgres.sh`

36. Remove asyncpg result handling tests once asyncpg removed.
Evidence: `ideas/evidence/36_evidence_test_qa_fiche_asyncpg.sh`

37. Remove advisory-lock support tests after SQLite-only pivot.
Evidence: `ideas/evidence/37_evidence_test_fiche_state_recovery_postgres.sh`

38. Revisit timeseries compatibility tests tied to Postgres assumptions.
Evidence: `ideas/evidence/38_evidence_test_ops_service_timeseries.sh`

39. Remove mutable ToolRegistry singleton once tests updated.
Evidence: `ideas/evidence/39_evidence_tool_registry_mutable_singleton.sh`

40. Drop non-lazy binder compatibility path.
Evidence: `ideas/evidence/40_evidence_lazy_binder_compat.sh`

41. Remove deprecated publish_event_safe wrapper.
Evidence: `ideas/evidence/41_evidence_events_publisher_deprecated.sh`

42. Require envelope-only WS messages, remove legacy wrapping.
Evidence: `ideas/evidence/42_evidence_websocket_legacy_wrap.sh`

43. Remove legacy admin routes without api prefix.
Evidence: `ideas/evidence/43_evidence_admin_legacy_router.sh`

44. Remove deprecated workflow start route.
Evidence: `ideas/evidence/44_evidence_workflow_exec_deprecated_route.sh`

45. Remove __APP_READY__ legacy test signal once tests updated.
Evidence: `ideas/evidence/45_evidence_app_ready_legacy_signal.sh`

46. Drop legacy React Flow selectors in CSS after test update.
Evidence: `ideas/evidence/46_evidence_canvas_react_legacy_selectors.sh`

47. Remove legacy buttons.css compatibility layer.
Evidence: `ideas/evidence/47_evidence_buttons_css_legacy.sh`

48. Remove legacy modal pattern CSS.
Evidence: `ideas/evidence/48_evidence_modal_css_legacy.sh`

49. Remove legacy util margin helpers once migrated.
Evidence: `ideas/evidence/49_evidence_util_css_legacy.sh`

50. Remove legacy token aliases after CSS migration.
Evidence: `ideas/evidence/50_evidence_tokens_css_legacy_aliases.sh`

51. Remove deprecated TextChannelController.
Evidence: `ideas/evidence/51_evidence_text_channel_controller_deprecated.sh`

52. Remove deprecated session handler API.
Evidence: `ideas/evidence/52_evidence_session_handler_deprecated.sh`

53. Remove compatibility methods in feedback system.
Evidence: `ideas/evidence/53_evidence_feedback_system_compat.sh`

54. Remove deprecated heuristic or hybrid decision modes in roundabout monitor.
Evidence: `ideas/evidence/54_evidence_roundabout_monitor_deprecated_modes.sh`

55. Remove HEURISTIC or HYBRID decision modes in LLM decider.
Evidence: `ideas/evidence/55_evidence_llm_decider_deprecated_modes.sh`

56. Split oikos_tools.py (large file).
Evidence: `ideas/evidence/56_evidence_oikos_tools_size.sh`

57. Split commis_resume.py (large file).
Evidence: `ideas/evidence/57_evidence_commis_resume_size.sh`

58. Split oikos_react_engine.py (large file).
Evidence: `ideas/evidence/58_evidence_oikos_react_engine_size.sh`

59. Split oikos_service.py (large file).
Evidence: `ideas/evidence/59_evidence_oikos_service_size.sh`

60. Split roundabout_monitor.py (large file).
Evidence: `ideas/evidence/60_evidence_roundabout_monitor_size.sh`

61. Split models/models.py (large file).
Evidence: `ideas/evidence/61_evidence_models_models_size.sh`

62. Split commis_runner.py (large file).
Evidence: `ideas/evidence/62_evidence_commis_runner_size.sh`

63. Split fiche_runner.py (large file).
Evidence: `ideas/evidence/63_evidence_fiche_runner_size.sh`

64. Split routers/agents.py (large file).
Evidence: `ideas/evidence/64_evidence_agents_router_size.sh`

65. Split main.py (large file).
Evidence: `ideas/evidence/65_evidence_main_py_size.sh`

66. Split AdminPage.tsx (large file).
Evidence: `ideas/evidence/66_evidence_admin_page_size.sh`

67. Split CanvasPage.tsx (large file).
Evidence: `ideas/evidence/67_evidence_canvas_page_size.sh`

68. Split oikos-chat-controller.ts (large file).
Evidence: `ideas/evidence/68_evidence_oikos_chat_controller_size.sh`

69. Split useOikosApp.ts (large file).
Evidence: `ideas/evidence/69_evidence_use_oikos_app_size.sh`

70. Split FicheSettingsDrawer.tsx (large file).
Evidence: `ideas/evidence/70_evidence_fiche_settings_drawer_size.sh`

71. Split or remove DashboardPage.tsx (large file).
Evidence: `ideas/evidence/71_evidence_dashboard_page_size.sh`

72. Split SessionsPage.tsx into smaller components.
Evidence: `ideas/evidence/72_evidence_sessions_page_size.sh`

73. Split ForumCanvas.tsx (large file).
Evidence: `ideas/evidence/73_evidence_forum_canvas_size.sh`

74. Split icons.tsx mega-file.
Evidence: `ideas/evidence/74_evidence_icons_file_size.sh`

75. Split commis-progress-store.ts (large file).
Evidence: `ideas/evidence/75_evidence_commis_progress_store_size.sh`

76. Move builtin tools to plugins to keep core lean.
Evidence: `ideas/evidence/76_evidence_builtin_tools_sprawl.sh`

77. Move or remove legacy ssh_tools from core.
Evidence: `ideas/evidence/77_evidence_ssh_tools_legacy.sh`

78. Simplify unified_access legacy behavior.
Evidence: `ideas/evidence/78_evidence_unified_access_legacy.sh`

79. Update Swarmlet user-agent branding in web_fetch tool.
Evidence: `ideas/evidence/79_evidence_web_fetch_swarmlet_user_agent.sh`

80. Remove legacy ToolRegistry wiring in builtin tools init.
Evidence: `ideas/evidence/80_evidence_builtin_init_legacy_registry.sh`

81. Simplify fiche_memory_tools SQLite compatibility filtering with better schema or indexes.
Evidence: `ideas/evidence/81_evidence_fiche_memory_sqlite_compat.sh`

82. Consolidate multiple memory tool modules into one API.
Evidence: `ideas/evidence/82_evidence_memory_tools_duplication.sh`

83. Consolidate runner_tools and task_tools overlap.
Evidence: `ideas/evidence/83_evidence_runner_task_tools_overlap.sh`

84. Move container_tools out of core if containerized commis is optional.
Evidence: `ideas/evidence/84_evidence_container_tools_in_core.sh`

85. Pluginize connector tools to keep OSS core lean.
Evidence: `ideas/evidence/85_evidence_connector_tools_sprawl.sh`

86. Consider merging web_search and web_fetch into a single web tool.
Evidence: `ideas/evidence/86_evidence_web_tools_overlap.sh`

87. Simplify jobs registry import graph to a single jobs pack.
Evidence: `ideas/evidence/87_evidence_jobs_registry_imports.sh`

88. Move jobs/examples out of production code.
Evidence: `ideas/evidence/88_evidence_jobs_examples_folder.sh`

89. Move jobs/qa out of production code.
Evidence: `ideas/evidence/89_evidence_jobs_qa_folder.sh`

90. Consolidate scheduler_service.py and workflow_scheduler.py.
Evidence: `ideas/evidence/90_evidence_scheduler_modules_overlap.sh`

91. Evaluate merging task_runner with scheduler service.
Evidence: `ideas/evidence/91_evidence_task_runner_scheduler_overlap.sh`

92. Merge commis_job_queue and commis_job_processor modules.
Evidence: `ideas/evidence/92_evidence_commis_queue_processor_split.sh`

93. Consolidate commis artifact and tool output stores into one subsystem.
Evidence: `ideas/evidence/93_evidence_commis_artifact_store_overlap.sh`

94. Remove TODO in oikos_runs router by implementing filter in CRUD.
Evidence: `ideas/evidence/94_evidence_oikos_runs_todo.sh`

95. Remove TODO cron parsing in oikos_fiches router by moving to scheduler module.
Evidence: `ideas/evidence/95_evidence_oikos_fiches_todo.sh`

96. Remove legacy trigger key scanner once legacy shapes dropped.
Evidence: `ideas/evidence/96_evidence_legacy_trigger_check_script.sh`

97. Remove legacy trigger upgrade logic in schemas/workflow.py.
Evidence: `ideas/evidence/97_evidence_workflow_schema_legacy_upgrade.sh`

98. Remove deprecated trigger_type field in workflow_schema.py.
Evidence: `ideas/evidence/98_evidence_workflow_schema_deprecated_trigger_type.sh`

99. Tighten trigger_config schema by removing extra allow compatibility.
Evidence: `ideas/evidence/99_evidence_trigger_config_extra_allow.sh`

100. Split generated openapi-types.ts to reduce bundle weight.
Evidence: `ideas/evidence/100_evidence_openapi_types_size.sh`
