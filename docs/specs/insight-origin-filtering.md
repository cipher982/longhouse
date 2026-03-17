# Insight Origin Filtering

## Goal

Make the remaining insight store more trustworthy by separating human-meaningful learnings from operational alert noise.

## Scope

- Add explicit `Insight.origin` values: `manual`, `reflection`, `system`
- Tag all current runtime writers with the right origin
- Backfill obvious existing system rows during startup-safe SQLite migration
- Exclude `system` rows from default insight reads and briefing/reflection context reads
- Keep a simple opt-in to include system rows when needed

## Non-goals

- Moving alerts to a separate table
- Reclassifying every historic reflection row
- Reworking the proposals product surface

## Design

- `POST /api/insights` writes `origin=manual`
- Reflection writer writes `origin=reflection`
- Stale-agent and ingest-health jobs write `origin=system`
- Default list queries hide only rows with `origin=system`
- Legacy rows with `origin IS NULL` stay visible
- Startup migration adds the new column and backfills the known system rows:
  - `tags` containing `stale-agent`
  - title `Stale ingest detected`
  - title `Ingest recovered`

## Success Criteria

- New insight rows always have an origin
- Existing system alerts stop appearing in normal insight reads after deploy
- Briefings stop feeding system alerts back into AI context by default
- Backend tests cover migration, filtering, and writer tagging
