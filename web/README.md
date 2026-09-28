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
