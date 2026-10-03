# web

The React/TypeScript UI that the Runtime Host serves, plus the source of the
iOS transcript document. Folder-by-folder layout: the Code map in
[`ARCHITECTURE.md`](../ARCHITECTURE.md#code-map).

- `src/app/` is the entry and shell, `src/features/<name>/` holds one product
  area each, `src/shared/` holds what several features use.
- Cross-folder imports use `@/`; `scripts/check-layout.mjs` rejects file-type
  roots such as `src/components/`.
- `src/generated/` is written by generators; never edit it.

Run: `make dev` (UI against your Runtime Host) or `make dev-demo` (seeded local
backend). Test: `make test-frontend`. See a page: `make ui-capture PAGE=timeline`.

The app navigation is Timeline and Machines. Machines owns provider readiness,
sign-in and sync diagnostics; factory proof assertions remain machine API
diagnostics, not a customer Admin page. A failed activity/sync read keeps the
independently available directory and machine actions visible. Cached facts are
marked last known when refresh fails; missing import progress is not completion.
Machine filters apply before lexical and semantic result limits, including retained
history whose display metadata lacks a device ID. The disposable search index adds
nullable machine identity and uses retained source identity for existing rows; source
history and embedding vectors are not rewritten.

For ended managed terminal sessions, **Show resume command** appears beside the
ended-run notice in the composer. It opens a command dialog; it does not restart
the agent. Run the copied command in a terminal on the named machine to continue
the same session and conversation. The action also appears when a run ends while
the page is open; an unsent draft is preserved when messaging becomes available again.

OMP and Pi use their native `longhouse <provider> --resume-session` entry points
with the original session ID and working directory, not a branch or remote launch.

On wide screens the turn outline follows the visible transcript and pins a clicked
turn through its smooth scroll. New live output does not move a reader away from
older content; loading older history preserves the viewport without counting it
as new output. The transcript gutters and readout rail belong to its scroll pane.

Hearth reel (the real timeline over scripted mock sessions, for the landing
page and posts): preview live at `/hearth-reel.html` under `bun run dev`;
record with `bun run record:hearth-reel` → `video/out/hearth-reel.mp4` plus a
poster PNG (1920x1080, 60 fps, no audio), zoomed onto the "Live now" rows. It
steps a virtual clock frame by frame, so the output never drops frames. Edit
`src/dev/hearth-reel/scene.ts` to change the story; `--frame page` records the
whole page, `--size`, `--fps 30` and `--out` retarget it.
