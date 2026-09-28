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

Hearth reel (the real timeline over scripted mock sessions, for the landing
page and posts): preview live at `/hearth-reel.html` under `bun run dev`;
record with `bun run record:hearth-reel` → `video/out/hearth-reel.mp4` plus a
poster PNG (1920x1080, 60 fps, no audio), zoomed onto the "Live now" rows. It
steps a virtual clock frame by frame, so the output never drops frames. Edit
`src/dev/hearth-reel/scene.ts` to change the story; `--frame page` records the
whole page, `--size`, `--fps 30` and `--out` retarget it.
