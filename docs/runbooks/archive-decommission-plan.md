# Archive Decommission Plan

This is the Phase 8 plan for reclaiming legacy raw storage after archive
restore has been proven. It is not approval to delete, compact, vacuum, or
rewrite production data.

## Preconditions

All of these must be true before any storage reclaim command is proposed:

- backup gate evidence in `docs/specs/reliability-data-plane.md` is still
  accepted or has been refreshed;
- legacy raw exporter completed without unreviewed corruption skips;
- archive chunk verification passes for `source_lines` and `events`;
- event-stream replay handles both legacy-exported and live archive-primary
  `events.legacy_ref` shapes;
- clean-store restore drill passes;
- timeline, detail, search, health, launch, heartbeat, and control smoke checks
  pass against the restored stores;
- architecture review has no high or medium issues for the restore/decommission
  plan;
- the maintainer explicitly approves the exact reclaim action and target tenant.

## Clean Restore Drill

Use a separate volume or temporary data root. Never run the drill by mutating the
live hosted DB.

1. Create empty hot/manifest and derived stores.
2. Restore sealed archive manifests and minimal session rows from the archive
   files.
3. Run the hot-card projector from `source_lines` chunks.
4. Run the derived event/search projector from `source_lines` chunks.
5. Replay the `events` stream and verify both `legacy_export` and
   `live_archive_primary` record shapes are understood.
6. Smoke restored behavior:
   - timeline/session list has restored cards;
   - detail projection has expected event rows;
   - FTS search finds known restored content;
   - health/launch/control paths do not require archive or derived stores.

The current automated fixture for this drill is
`server/tests_lite/test_archive_restore.py`.

## Retention Plan

Keep all three raw-data copies until the restore drill and maintainer approval
are both complete:

- old monolith raw tables (`source_lines`, raw `events` blobs);
- sealed archive files;
- off-volume backup/snapshot from the backup gate.

After approval, prefer a clean-store cutover over in-place SQLite surgery:

1. Build clean hot/derived stores from archive.
2. Stop the target Runtime Host.
3. Move the old DB aside as read-only retained evidence.
4. Start the Runtime Host against the clean stores.
5. Run live smoke checks.
6. Keep the old DB and backup for a fixed retention window before deletion.

Do not reclaim space by deleting rows from the live monolith or running live
`VACUUM`. Those are separate high-risk operations and require their own written
approval.

## Explicit Approval Gate

The final reclaim command must be written down before it runs and must include:

- target tenant/subdomain;
- exact backup path or snapshot id;
- exact archive root;
- exact old DB path;
- exact new hot/derived DB paths;
- expected rollback move/rename commands;
- maintainer approval line.

No agent should infer this approval from the existence of this runbook, green
tests, successful export, or prior broad instruction to continue the epic.

## Rollback

If clean-store smoke fails:

1. Stop the Runtime Host.
2. Restore the old DB path/name.
3. Restart the Runtime Host.
4. Verify `/api/readyz`, timeline/session list, and ingest.
5. Keep archive and failed clean stores for forensic comparison.
